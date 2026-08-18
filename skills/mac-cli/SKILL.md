---
name: mac-cli
description: How the mac CLI is actually shaped — object groups, the admin re-parenting, and the verbs that are not what you would guess. Read this before running mac commands.
---

# The mac CLI

`mac <object> <verb> [args]`. Everything is an object with verbs, and the
objects that matter day to day are `project`, `task`, `work-package` and
`agent`. Everything else lives under `admin`.

`--json` works in any position: `mac task list --json` and `mac --json task
list` are the same command.

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
destroying tasks. `mac work-package delete` is `cancel`. Read the help before
assuming a delete is destructive — or that it is not.

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
