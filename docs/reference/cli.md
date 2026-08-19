# Command-line reference

This page is generated from the current parser. Do not edit it directly.
The book uses executable `bash` blocks; reference usage is rendered as output.

## mac

```console
$ mac --help
usage: mac [-h] [--version] [--db DB] [--local-authority] [--hub-url HUB_URL]
           [--token TOKEN] [--fleet FLEET] [--profile PROFILE] [--json]
           SUBCOMMAND ...

Multi-agent coordinator control plane

positional arguments:
  SUBCOMMAND

options:
  -h, --help         show this help message and exit
  --version          print the mac version and exit
  --db DB            direct PostgreSQL control-plane authority (a postgres://
                     DSN) for hub maintenance, standalone development, tests,
                     and migration. It is not a repository ticket store or
                     offline hub replica and never synchronizes with a hub.
                     When unset and no hub is configured, mac refuses to run.
  --local-authority  enable stopped-hub maintenance against the authoritative
                     PostgreSQL database selected by --db or MAC_DB. The
                     command refuses this mode while the configured hub health
                     endpoint is reachable.
  --hub-url HUB_URL  MAC hub URL (hub mode). Falls back to $MAC_API_URL /
                     $MAC_URL / $MAC_HUB_URL, then ~/.mac/fleets.yaml for
                     --fleet.
  --token TOKEN      Bearer token for hub mode. Falls back to $MAC_API_TOKEN
                     (or $MAC_API_TOKEN__<FLEET> when --fleet is set).
  --fleet FLEET      Fleet name; selects MAC_API_TOKEN__<FLEET> and
                     ~/.mac/fleets.yaml entry.
  --profile PROFILE  Secure client profile under ~/.mac/clients. Falls back to
                     $MAC_PROFILE or the active profile.
  --json             Emit JSON instead of the default human-readable text.
                     Works in any position (e.g. `mac task list --json` or
                     `mac --json task list`).

The objects mac models. Start here:

  project  a unit of work ownership: repositories, policy, dispatch state
  task     one unit of work: the thing agents claim, run, and publish
  agent    a worker that claims and executes tasks on a machine

  Each supports: create, list, show, update, delete
  `mac <object> help` lists its commands; `mac <object> help <subcommand>`
  shows the arguments that subcommand takes.

Everything else:
  admin  fleet, runtime and control-plane administration

0 administrative commands live under `mac admin` (`mac admin help` lists them).
They moved: `mac fleet ...` is now `mac admin fleet ...`, and the old spelling says so.

Run `mac help --all` to see every command in one list.
```

## mac task

```console
$ mac task --help
usage: mac task [-h] SUBCOMMAND ...

positional arguments:
  SUBCOMMAND

options:
  -h, --help  show this help message and exit

task -- one unit of work: the thing agents claim, run, and publish

CRUD:
  create  file a new task into the ledger
  list    list tasks (default: short ids; use --full-ids for scripts)
  show    show task state, activity, diagnoses, and compact evidence counts (use --json for the complete structured record)
  update  change a task's fields (title, description, project, priority, capabilities, metadata, max attempts)
  delete  actively cancel a task, revoke its lease, and abort a running worker executor (same as `cancel`)

Finding work:
  ready          list task-ready work and the number of currently eligible fleet agents
  search         keyword search across task title and description
  stats          count tasks by state
  why-unclaimed  show the authoritative task and agent reasons preventing a claim
  summary        activity-only task narrative (also included in `task show`)

Execution:
  claim    atomically claim a task for an agent
  start    move a claimed task to RUNNING as its lease holder
  release  clear a --no-dispatch hold so the task can auto-dispatch
  close    transition a task to completed/cancelled; cancellation requires a reason
  reopen   recovery: return a stuck/terminal task (failed/cancelled/blocked) to OPEN for retry or reconciliation

Review and evidence:
  evidence        attach evidence to a task: the record a review and auto-land read
  submit-review   hand a running task to the adversarial reviewer (to NEEDS_REVIEW)
  force-complete  BREAK-GLASS operator override: mark a task COMPLETED regardless of state/review (bypasses the adversarial auto-land gate; audited). Not the normal path — the adversarial reviewer + contract gate auto-land is.
  audit           read-only reconciliation of every task's history, evidence, dependencies, replacements, and git ancestry

Human input:
  ask          park a task on an unanswered human question (not a failure; never reaped)
  answer       record the answer to a parked question and dispose of the task (resumes by default; --cancel when the answer is 'not needed')
  needs-input  list tasks parked on an unanswered human question (the operator inbox)

Recovery:
  recover-stranded           re-supervise tasks left waiting on a terminal dependency (dry-run unless --apply)
  recover-finalizer          revalidate and publish preserved work refused for uncommitted new files
  recover-stalled-finalizer  resume a deterministic finalizer that stalled (timeout/cancel/crash) after harvesting verified work

Break-glass:
  break-glass         admin-only: authorize an exact task/agent pair for single-use direct host execution
  break-glass-list    list durable break-glass authorizations for a task
  break-glass-revoke  admin-only: revoke an unclaimed host authorization

Reporting:
  throughput       task-to-main KPIs, stage dwell, stranded work, and resource collisions
  generator-yield  show each task origin's completion yield and whether the yield gate is letting it file

Migration:
  detect-beads       inspect a repo for .beads/ artifacts (read-only)
  migrate-beads      import .beads/issues.jsonl into MAC tasks and emit .tickets/<id>.md
  detect-ticketing   detect ticketing sources in a repo (.tickets local mirror / .beads foreign) and whether a one-way conversion should be offered (read-only)
  convert-ticketing  one-way convert a detected foreign source (e.g. beads) into MAC ledger tasks plus optional local compatibility files (run after the user agrees)

Other:
  wait        wait for a project's tasks to finish, streaming each transition
  reassign    re-file tasks under a person (backfills a ledger with no filers)
  export      emit one task whole (record, history, coding-CLI session) as JSON
  transcript  the coding-CLI session for a task, in order
  preflight   would a task with these requirements ever be claimed?
  edit        answer a parked task in $EDITOR; saving submits it back to the queue
  select      preview the group of tasks a selector expression names
  batch       apply one operation to every task a selector names (dry by default)
  group       named, saved task groups
  egress      declare which hosts a task's sandbox may reach

Run `mac task help <subcommand>` for the arguments one takes.
```

## mac project

```console
$ mac project --help
usage: mac project [-h] SUBCOMMAND ...

positional arguments:
  SUBCOMMAND

options:
  -h, --help  show this help message and exit

project -- a unit of work ownership: repositories, policy, dispatch state

CRUD:
  create  create a project and its dispatch policy
  list    list projects with live work or a registration
  show    show one project: policy, repositories, dispatch state
  update  update project fields or its branch-qualified repository registration
  delete  remove a project; --force detaches historical tasks and disables linked checkout registrations (same as `unregister`)

Dispatch:
  pause     hold a project's tickets from autonomous dispatch
  activate  open a project to autonomous dispatch

Repositories:
  register  register the current checkout, a local path, or GIT_URL[#BRANCH] as a project and create its contract-authoring task

Other:
  egress  hosts every task in this project may reach from its sandbox

Run `mac project help <subcommand>` for the arguments one takes.
```

## mac agent

```console
$ mac agent --help
usage: mac agent [-h] SUBCOMMAND ...

positional arguments:
  SUBCOMMAND

options:
  -h, --help  show this help message and exit

agent -- a worker that claims and executes tasks on a machine

CRUD:
  create  register a new agent onto a machine (same as `register`)
  list    list agents (--health adds liveness)
  show    show one agent
  update  change an agent's capabilities, status or metadata
  delete  decommission (tombstone) an agent: strips operational overlays (moods/naps/config flags/deploy config) but preserves its AgentBus streams, events, deliveries, and task history

Availability:
  hold        place a dispatch hold on an agent; held agents are skipped during claim-next
  resume      remove the dispatch hold from an agent, making it eligible for dispatch again
  heartbeat   report an agent alive, with its status, health, and resources
  deregister  graceful exit for a session/ephemeral agent: optionally leave one final human-facing message (delivered after the agent is gone), then tombstone with history preserved

Inspection:
  config    consolidated per-agent configuration (the 'geek knobs')
  hardware  fleet hardware inventory from self-reported resources.hardware
  reflect   publish an agent's runtime self-description over AgentBus

Communication:
  tell  send a hub-verified HUMAN directive to any agent over AgentBus — works for Slack-less agents (GKE runners, ephemeral sessions); the receiver can trust its operator provenance by construction

Administration:
  attestation-recover      admin-only conditional recovery for a missing/stale worker signing key
  report-executor-approve  approve the exact current startup-attested OpenShell report executor
  report-executor-revoke   revoke report-repository dispatch eligibility for an agent
  migrate                  move an agent (soul + memory) to a new host; dry-run unless --execute

Run `mac agent help <subcommand>` for the arguments one takes.
```

## mac admin

```console
$ mac admin --help
usage: mac admin [-h] SUBCOMMAND ...

fleet, runtime and control-plane administration

positional arguments:
  SUBCOMMAND

options:
  -h, --help  show this help message and exit

admin -- fleet, runtime and control-plane administration

Getting started:
  init         create the control-plane schema in a PostgreSQL store
  login        authenticate this machine against a hub
  logout       discard stored hub credentials
  config       read and migrate local mac configuration
  diagnostics  run read-only control-plane health checks

Fleet and machines:
  fleet          deploy, inspect and maintain the fleet as a whole
  machine        hosts that agents run on
  hgx            HGX / GPU capacity management
  openshell      sandboxed execution environments for agents
  mcp            serve the ledger to coding agents as Model Context Protocol tools
  sandbox-image  the sandbox IMAGE: its bill of materials and its rollout
  runtime        runtime images and environment definitions
  rollout        staged rollout of a runtime or configuration
  env            environment variables projected onto fleet hosts
  secret         secret storage, rotation and access audit
  database       control-plane database maintenance
  migrate        schema and data migrations

Getting work done:
  dispatch      the loop that matches ready tasks to eligible agents
  review        adversarial review of completed work
  publish       publish reviewed work to its destination
  pull-request  pull requests raised from task work
  workflow      multi-step workflow definitions and runs
  plan          planning helpers, including dependency ordering
  eval          evaluation runs over agent output
  optimizer     model and routing optimization
  repo          repositories that tasks execute against
  artifact      durable artifacts produced by task work

What agents know:
  memory           durable cross-session knowledge
  journal          per-agent narrative history
  mood             agent temperament and its effect on execution
  nap              consolidation cycles that summarize recent work
  dream            offline pattern-finding over past work
  curiosity        quarantined self-proposed experiments awaiting judgment
  human-interface  port an agent profile between Hermes and OpenClaw
  persona          Hermes personas and their memory scopes

Talking to people and systems:
  message        messages between agents and humans
  agentbus       the agent-to-agent message bus
  communication  communication channels and routing
  notifier       outbound notification channels
  directive      operator directives issued to agents
  hermes         Hermes instances and their context
  binding        Hermes platform bindings
  interaction    durable work created from a conversation
  bridge         external system bridges
  integrations   third-party integrations

Who can do what:
  tenant  tenant boundaries
  human   people who own agents and file tasks
  user    tenant-scoped user identities
  client  API clients and their principals

Seeing what happened:
  events         the unified event stream
  action-events  recorded agent actions
  observability  structured metrics and logs
  command-audit  audit of commands agents ran

Run `mac admin help <command>` for the arguments one takes.
These moved here from the top level; `mac <command>` now redirects.
```

## mac help

```console
$ mac help --help
usage: mac help [-h] [--all] [SUBCOMMAND]

positional arguments:
  SUBCOMMAND  scope help to one subcommand and show the arguments it takes

options:
  -h, --help  show this help message and exit
  --all       list every command, not just the common ones
```
