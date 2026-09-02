---
name: mac-cli
description: How the mac CLI is actually shaped — object groups, the admin re-parenting, and the verbs that are not what you would guess. Read this before running mac commands.
---

# The mac CLI

## What this skill owns, and what it does not

It owns the CLI's shape: which object a verb hangs off, which verbs mean
something other than their name, what the default views hide, and the traps
that have cost a wrong command against a live fleet.

It does **not** own: architecture decisions (those are `docs/adr/`), how to
diagnose a failed task, how to decompose work, or repository conventions
(`CLAUDE.md`, `CONTRIBUTING.md`). If a question is "what should this task be",
this is the wrong document.

Stating the boundary is worth the four lines: a skill that quietly grows into
everything stops being read, and the reader cannot tell what it is responsible
for being right about.

## Before acting after a compaction or handoff

If this conversation has been compacted, summarised, or handed over, **reread
this skill and the live state before any mutating command** — do not act on a
summary's recollection of either.

This is not hypothetical caution. In one session a task was reopened from a
summary that did not record that its work had already merged, and an agent
immediately claimed it and began re-implementing a merged module. The summary
was accurate about what had happened; it just did not carry the one fact that
made the action wrong.

Cheap checks, in order: `mac task show <id>` for current state, and
`gh pr list --search "<task_id>" --state all` for work that already landed.

`mac <object> <verb> [args]`. Everything is an object with verbs, and the
objects that matter day to day are `project`, `task` and `agent`. Everything
else lives under `admin`.

Non-interactive stdout defaults to JSON and disables pagers. Interactive
terminals default to compact text. `--json` explicitly selects JSON in any
position: `mac task list --json` and `mac --json task list` are the same
command.

## If you are registered as an agent, send your own heartbeat

An interactive session that registers itself as a runner is a real agent on a
real host, and **nothing will report your liveness for you**. The hub stamps
only `resources.virtual` agents (`operator`, `hub-reviewer`) — constructs whose
liveness the hub itself constitutes. Vouching for a session on someone's
laptop would describe a closed lid as a healthy worker, so it deliberately
does not.

If you do not heartbeat, the stale sweep marks you `offline`. Observed
2026-08-20: a session sat `offline` for 33 minutes while actively working, and
the operator's `mac agent list` said it was gone.

    mac agent heartbeat <agent_id> --status idle --health-status healthy

Send one when you register, and again at natural checkpoints in a long
session — after landing a change, before a long-running gate.

**The id is not the name.** The name keeps hyphens, the id substitutes
underscores: name `claude-session-yowza`, id `agent_claude_session_yowza`.
`mac agent show` takes the id and answers `agent not found` for the name, which
reads like the agent is gone rather than misaddressed. `mac --json agent list`
gives you both.

A hold survives a heartbeat, and should: a held session reports itself alive
without becoming eligible for dispatch.

## The traps

These are not hypothetical. Each one cost a wrong command against a live fleet.

**Everything unfamiliar is under `admin`.** The top level was deliberately
reduced to the four objects above. `dispatch`, `human`, `memory`, `machine`,
`fleet`, `client`, `openshell`, `secret` and the rest all moved. `mac dispatch`
prints a redirect rather than working, so if a command "used to exist", try
`mac admin <thing>` before assuming it was removed.

**A paused project is `activate`d, not `resume`d.** `mac project resume` does
not exist. Agents use the opposite pair: `mac agent hold` / `mac agent resume`.
So the verb depends on the object, and guessing from the other one is wrong.

**`mac agent install` is not a verb.** `mac agent` is the fleet worker:
register, list, hold, resume, heartbeat. The harness installer is
`mac admin plugin install`. Guessing `mac agent install` from a design
doc that said "agent install" sends you to a usage error.

**`mac agent update` cannot change status.** It takes `--capabilities`,
`--add-capability`, `--remove-capability`, `--instance-kind`, `--owner`,
`--visibility`. To clear a `draining` agent, PUT `{"status": "idle"}` to
`/agents/{id}` on the hub.

**`mac project pause` only reaches REGISTERED projects.** A project name that
came from task metadata rather than `mac project create` returns `Not Found`,
so pausing "the fleet" by walking the project list silently misses work. To
stop dispatch fleet-wide, hold the agents.

**Pausing a project does not drain it.** In-flight tasks keep running. A deploy
started at that moment fails with `active work attached while release
compensation ran`. Wait until no agent reports `current_task_id`.

**`delete` is usually a rename of something gentler.** `mac project delete` is
`unregister`; with `--force` it sets `tasks.project = NULL` rather than
destroying tasks. Read the help before assuming a delete is destructive — or
that it is not.

## Lists are scoped by default

`list` shows what you can still act on, not the whole table. Both defaults exist
because the unscoped view was unreadable on a real hub.

    mac task list                  active work only (17 rows on the live hub)
    mac task list --all-states     every task, terminal included (507)
    mac task list --state=cancelled   one state, unchanged

    mac project list               projects with live work OR a registration (10)
    mac project list --all         every project, including derived ghosts (19)

**When to pass the flag.** `--all-states` / `--all` are for auditing and
archaeology — counting what a project ever held, finding a task you cancelled
last week, verifying a prune. For anything about what the fleet is doing now,
the default is the answer, and a flag that adds 490 cancelled rows is actively
misleading.

**What `project list --all` reveals.** A project with no `project_id` is
DERIVED: it exists only because some task carries that string. `mac project
show`, `pause`, `activate` and `unregister` all return `Not Found` for one, and
nothing can be dispatched to it. It is a label, not an object. That is why it is
hidden unless it holds live work.

## How the project is inferred

`mac task create` with no `--project` infers one from the working directory: the
name of the repository, resolved through `git rev-parse --git-common-dir`, which
every linked worktree shares.

This matters because CLAUDE.md mandates a worktree per agent. Inferring from
`--show-toplevel` instead — which is what this used to do — takes the basename
of the WORKTREE, so filing from `/tmp/mac-dev` created a project called
`mac-dev`. Six such ghosts were live on the hub, each holding one or two
cancelled tasks. If you see a project named after a branch, that is what it is.

Pass `--project ''` for none, or `--project NAME` to override.

## Finding things

    mac help                      the object list
    mac <object> help             verbs for one object
    mac admin help                everything that moved under admin
    mac <object> <verb> --help    arguments

`mac task create help` is intercepted and prints help; it does not file a task
called "help".

## The commands that come up most

    mac task list --state=open          mac task show <id>
    mac task ready --limit 10           mac task why-unclaimed <id>
    mac task create "title" --description-file=f.txt
    mac task reopen <id>                recovery: terminal/stuck -> open
    mac agent list                      mac agent hold/resume <id>
    mac project pause/activate <name>
    mac admin human register <username>
    mac admin dispatch submit <file>    literate-ai execution requests
    mac admin judgement status          process-quality daemon last report
    mac admin judgement run             run one judgement cycle now
    mac admin events news --follow      significant task and agent activity
    mac admin login --local-console     hub-local enrollment without SSH
    mac admin plugin install --scope global
    mac admin plugin install --scope repo --repo PATH
    mac admin plugin status
    mac admin plugin uninstall

`mac admin events news` is the human-facing fleet feed: task creation, claim and
state changes plus meaningful agent lifecycle/status changes. Add `--follow` to
subscribe continuously, `--project NAME` to restrict task activity to one
project, or `--json` for newline-delimited records in follow mode.

`mac admin login --local-console` is only for a shell on the hub. It asks the
running API service for a new scoped, independently revocable credential over
its kernel-authenticated Unix socket; it never reads the shared hub admin token.
The ordinary scope boundary is `read,write,dispatch`. Any broader scope requires
the API service's OS account or root plus `--allow-elevated`; a configured
supplementary group grants ordinary enrollment only. Remote clients continue to
use `mac admin login --ssh ...` or a registered fleet SSH route.
Use `mac admin login renew --local-console` to rotate a direct local-console
profile through the same socket; the new bearer is validated before replacing
the local credential.

## File tasks in dependency order, because there is no second chance

`mac task create` takes `--dependencies`. **`mac task update` does not.** Only
creation can establish an edge, so a task filed without one can never be
sequenced afterwards through the CLI — you would have to `PUT /tasks/<id>`
against the hub with a dependencies array.

That matters because the ledger dispatches fast. A task filed now is claimed by
a fleet agent within minutes — sooner than it takes to file the next few tasks
and work out how they relate. There is no "file them all, sequence them after".

So when a batch of findings has an order:

1. File the gating task first — the ADR, the decision, the thing that must be
   settled before anything else is safe to touch.
2. Note its id.
3. Create each dependent task WITH `--dependencies` in the same command.

A dependent task enters `waiting` rather than `open`, and stops being claimable
until its blocker reaches a terminal state. That is the whole mechanism; it
simply cannot be applied retroactively.

**What happens if you skip this.** On 2026-08-20 eleven tasks were filed flat in
one session. Within the hour four were `reviewing`, four `failed`, one
`running` — including an unsupervised attempt at a 5,565-line Hermes removal
that should have been gated behind an ADR *and* behind resolving a vendored
directory the deploy still required. Deleting that code first would have made
the fleet undeployable. Two more tasks were being worked by agents while the
same defects were being fixed by hand in open PRs, because nothing connected the
filing to the fix.

Two habits that follow:

- **If you are also fixing the thing yourself, say so in the task at creation**,
  or do not file it. A detailed task plus a parallel fix is duplicated work, and
  the agent that claims it cannot know.
- **An investigation task and its implementation are two tasks with an edge**,
  not one task. File the investigation, then create the implementation depending
  on it.

## Priority is inverted, and 0 is the bottom

`priority` is an integer sorted DESCENDING — the allocator sorts on
`-int(task.priority)`. **Higher is more urgent.** The convention on the live
hub is:

    P0  ->  priority 100      the top of the open queue
    escalations above it      900, 1000, 2000 (used sparingly)

So "make this P0" means `--priority 100`. `mac task update <id> --priority 0`
does the exact opposite of what it reads like: it sends the task to the back
of the queue, behind everything. There is at least one `P0:`-titled task on
the hub sitting at priority 0 for this reason.

`mac task update <id> --priority N` is a targeted field write. It preserves
description, metadata, dependencies, capabilities, `max_attempts`, state and
project — verified across 18 tasks. You do not need to re-send the
description to change a priority, and you should not: passing
`--description-file` rewrites the whole field.

## If you are working the ledger, you are a participant — register and claim

This applies to YOU, an interactive session, not only to loop-mode workers.

An interactive session with hub credentials is a fleet participant whether or
not it is registered. If it is not registered, its work is invisible: no agent
row, no claim, no `owner_agent_id`, no `attempt_count`. Other agents cannot see
that the task is being worked, so nothing stops one of them starting the same
task. That is the same claim-exclusivity failure that produced five divergent
implementations of one task and eleven duplicate PRs on 2026-08-19 — an
unregistered session is one more way to cause it.

This was found by audit: a session on a host that was not any fleet node fixed
and merged two tasks (PR #478, PR #465), and hours later both still read
`state=open`, `owner_agent_id=None`, `attempt_count=0`.

So:

    mac admin machine register <host> --machine-id machine_<host>
    mac agent register machine_<host> <session-name> --agent-id agent_<name>
    mac task claim <task_id> <agent_id>      # BEFORE you start work

**Register held, and understand what the hold does.** An interactive session
must never be a dispatch target — it cannot answer an assignment. `mac agent
hold <id>` prevents that. `agent_operator` is already held for precisely the
failure to avoid: *"claimed task_1b76d5ed and held it 6h silently without
executing."* A claim you are not actively executing is worse than no claim,
because it stops an agent that would have done the work.

**Claim before, not after.** Claiming retroactively does not work, and the
state machine is what stops you: from `open` the only legal moves are `blocked,
cancelled, claimed, failed, needs_input, waiting` — `completed` is not among
them. Completion is reachable only through `claimed -> running -> needs_review`,
i.e. through the fleet's own review loop. Work finished outside that loop
cannot be marked completed at all; the only exit is `cancel` with an
explanatory reason, which files finished work under abandonment. Do not assume
this ledger's cancelled count means work was abandoned.

**Releasing a hold hands the task out immediately.** `mac task release` clears
a `--no-dispatch` hold (use it — do not hand-edit `metadata.no_dispatch`).
But if the task is already satisfied by merged work, an agent will claim it
within seconds and start re-implementing it. Confirm the work is genuinely
outstanding before releasing, and close it first if it is not.

## Before you work a task, check whether it already has a PR

A retried task currently files a NEW pull request on each attempt rather than
updating the existing one. On 2026-08-19 the open queue held 23 PRs covering
12 distinct pieces of work; one task had emitted five, by two different
agents, with five divergent implementations (+598 to +2285 lines).

Two consequences for anyone working a task:

- Search for the task id before starting. PR titles carry it, so
  `gh pr list --search "<task_id>"` finds prior attempts.
- Finding an open PR for your task does not mean the work is done. It means
  an earlier attempt got that far. Read it before writing a second one.

Until the idempotency fix lands, this is manual. It is the single largest
source of wasted fleet capacity observed to date.

## Reading agent state

**`mac agent show <name>` often fails for an agent that `mac agent list`
displays.** `show` resolves by id, and an agent registered before the current
id convention has an opaque uuid that matches neither its name nor the id the
code expects. `hub-reviewer` is the live example: it is
`agent_da539e43b3e147ffb580ebdd9f7cde6b`, while the constant in
`services.py` says `agent_hub-reviewer`, and both `mac agent show
hub-reviewer` and `mac agent show agent_hub-reviewer` return "agent not
found". Get the real id from `mac agent list --json`.

**Virtual agents flap.** `hub-reviewer` is registered by the hub itself so
hub-side contract verification has an identity to sign with. It has no worker
process, so nothing heartbeats for it, and `health_status` is only whatever
was last written. It moves between `idle`, `offline` and `degraded` with no
underlying fault. Do not diagnose it as a broken worker; it is not a worker.

**`why-unclaimed` may name no reason at all.** For a task that is genuinely
eligible and simply not being dispatched, it prints attempt counts and
nothing else. `attempt_count: 0` with idle capable agents and no dispatch
hold is a real state, and it means the fault is in dispatch rather than in
anything about the task. Do not read a bare `why-unclaimed` as "nothing is
wrong".
## Requirements: capabilities versus hardware

Capabilities are set membership over a DECLARED vocabulary — agents advertise
`python`, `testing`, `review`. Host facts are NOT capabilities: `os` and
`cpu_arch` are probed into `resources.hardware`, and no agent will ever
advertise `linux`. Asking for one as a capability produces a task that is
created and never claimed.

    WRONG   --capabilities linux
    RIGHT   --hardware '{"os": ["linux"], "cpu_arch": ["x86_64"]}'

`mac task preflight` answers whether the fleet could ever claim a task, and
names this mapping error specifically. Run it before filing anything with
requirements; a task that cannot be satisfied is accepted and queued, and then
waits forever rather than failing. Use `mac task why-unclaimed <id>` for one
that is already filed and sitting.

## Where mac is talking to

Resolution order: `--db` (direct Postgres, prints a redacted banner), then
`--hub-url`, then `$MAC_API_URL`/`$MAC_URL`/`$MAC_HUB_URL`, then `--fleet` via
`~/.mac/fleets.yaml`. Nothing configured is an error, never a silent default.

Not every ControlPlane method is wrapped for hub mode. "`X` is not yet
supported in hub mode" means the RemoteDispatch wrapper is missing, not that
the feature is absent — the hub route usually exists.
