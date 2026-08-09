# Command-line reference

This page is generated from the current parser. Do not edit it directly.
The book uses executable `bash` blocks; reference usage is rendered as output.

## mac

```console
$ mac --help
usage: mac [-h] [--db DB] [--local-authority] [--hub-url HUB_URL]
           [--token TOKEN] [--fleet FLEET] [--profile PROFILE] [--json]
           SUBCOMMAND ...

Multi-agent coordinator control plane

positional arguments:
  SUBCOMMAND

options:
  -h, --help         show this help message and exit
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

  project       a unit of work ownership: repositories, policy, dispatch state
  task          one unit of work: the thing agents claim, run, and publish
  work-package  a task group: several tasks assembled, certified and landed together
  agent         a worker that claims and executes tasks on a machine

  Each supports: create, list, show, update, delete
    except work-package, which has no delete or update
  `mac <object> help` lists its commands; `mac <object> help <subcommand>`
  shows the arguments that subcommand takes.

Getting started:
  init         create the control-plane schema in a PostgreSQL store
  login        authenticate this machine against a hub
  logout       discard stored hub credentials
  config       read and migrate local mac configuration
  diagnostics  run read-only control-plane health checks

Fleet and machines:
  fleet      deploy, inspect and maintain the fleet as a whole
  machine    hosts that agents run on
  openshell  sandboxed execution environments for agents
  secret     secret storage, rotation and access audit

Getting work done:
  dispatch  the loop that matches ready tasks to eligible agents
  review    adversarial review of completed work
  publish   publish reviewed work to its destination
  repo      repositories that tasks execute against

What agents know:
  memory  durable cross-session knowledge

37 more commands are available. `mac help --all` lists them, and every one of them
runs whether it is listed or not -- nothing is removed.

Run `mac help <command>` for any of them.
```

## mac diagnostics

```console
$ mac diagnostics --help
usage: mac diagnostics [-h] [--check CHECK]

run read-only control-plane health checks

options:
  -h, --help     show this help message and exit
  --check CHECK  run only the named check (repeatable)
```

## mac init

```console
$ mac init --help
usage: mac init [-h]

create the control-plane schema in a PostgreSQL store

options:
  -h, --help  show this help message and exit
```

## mac database

```console
$ mac database --help
usage: mac database [-h] {migrate-sqlite-to-postgres,help} ...

control-plane database maintenance

positional arguments:
  {migrate-sqlite-to-postgres,help}
    migrate-sqlite-to-postgres
                        copy every SQLite row to PostgreSQL and verify full-
                        row digests
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac config

```console
$ mac config --help
usage: mac config [-h] {migrate-env-namespace,help} ...

read and migrate local mac configuration

positional arguments:
  {migrate-env-namespace,help}
    migrate-env-namespace
                        add fleet-scoped variants of flat MAC_* credentials in
                        ~/.mac/.env (mac-g55y)
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac login

```console
$ mac login --help
usage: mac login [-h] [--ssh SSH_TARGET] [--ssh-port SSH_PORT]
                 [--proxy-jump PROXY_JUMP] [--identity-file IDENTITY_FILE]
                 [--known-hosts-file KNOWN_HOSTS_FILE]
                 [--host-key-fingerprint HOST_KEY_FINGERPRINT]
                 [--host-ca HOST_CA] [--fleet LOGIN_FLEET] [--agent AGENT]
                 [--fleets-config FLEETS_CONFIG] [--profile LOGIN_PROFILE]
                 [--client-id CLIENT_ID] [--name NAME] [--scopes SCOPES]
                 [--capabilities CAPABILITIES] [--expires-in EXPIRES_IN]
                 [--local-port LOCAL_PORT] [--remote-host REMOTE_HOST]
                 [--remote-port REMOTE_PORT] [--allow-elevated] [--rotate]
                 [--remote-mac REMOTE_MAC] [--connect-timeout CONNECT_TIMEOUT]
                 [{status,renew}]

authenticate this machine against a hub

positional arguments:
  {status,renew}        inspect or renew the selected login; omit to enroll

options:
  -h, --help            show this help message and exit
  --ssh SSH_TARGET      hub SSH target user@host
  --ssh-port SSH_PORT
  --proxy-jump PROXY_JUMP
  --identity-file IDENTITY_FILE
  --known-hosts-file KNOWN_HOSTS_FILE
  --host-key-fingerprint HOST_KEY_FINGERPRINT
  --host-ca HOST_CA
  --fleet LOGIN_FLEET
  --agent AGENT
  --fleets-config FLEETS_CONFIG
  --profile LOGIN_PROFILE
  --client-id CLIENT_ID
  --name NAME
  --scopes SCOPES
  --capabilities CAPABILITIES
  --expires-in EXPIRES_IN
  --local-port LOCAL_PORT
  --remote-host REMOTE_HOST
  --remote-port REMOTE_PORT
  --allow-elevated
  --rotate              replace an existing client identity/profile explicitly
  --remote-mac REMOTE_MAC
                        path to the mac executable on the hub; auto-discovered
                        when omitted
  --connect-timeout CONNECT_TIMEOUT
```

## mac logout

```console
$ mac logout --help
usage: mac logout [-h] [--profile LOGOUT_PROFILE] [--revoke]
                  [--remote-mac REMOTE_MAC]
                  [--connect-timeout CONNECT_TIMEOUT]

discard stored hub credentials

options:
  -h, --help            show this help message and exit
  --profile LOGOUT_PROFILE
  --revoke              revoke the hub credential before deleting local secret
                        state
  --remote-mac REMOTE_MAC
  --connect-timeout CONNECT_TIMEOUT
```

## mac client

```console
$ mac client --help
usage: mac client [-h] {enroll,renew,revoke,list,profile,help} ...

API clients and their principals

positional arguments:
  {enroll,renew,revoke,list,profile,help}
    enroll              hub-local: mint a revocable scoped credential (invoke
                        through SSH)
    renew               hub-local: rotate one client's token and expiry
    revoke              hub-local: immediately revoke one client credential
    list                hub-local: list client principals without token hashes
    profile             install, select, inspect, or remove local secure
                        profiles
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac tenant

```console
$ mac tenant --help
usage: mac tenant [-h] {register,list,help} ...

tenant boundaries

positional arguments:
  {register,list,help}
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac user

```console
$ mac user --help
usage: mac user [-h] {register,help} ...

human user identities

positional arguments:
  {register,help}
    help           show help for this command group

options:
  -h, --help       show this help message and exit
```

## mac persona

```console
$ mac persona --help
usage: mac persona [-h] {register,help} ...

Hermes personas and their memory scopes

positional arguments:
  {register,help}
    help           show help for this command group

options:
  -h, --help       show this help message and exit
```

## mac human-interface

```console
$ mac human-interface --help
usage: mac human-interface [-h] {port,check,help} ...

port an agent profile between Hermes and OpenClaw

positional arguments:
  {port,check,help}
    port             port identity, memory and messaging credentials between
                     interfaces
    check            report whether switching to an interface would lose the
                     profile
    help             show help for this command group

options:
  -h, --help         show this help message and exit
```

## mac hermes

```console
$ mac hermes --help
usage: mac hermes [-h] {register,context,work-context,runtime-proof,help} ...

Hermes instances and their context

positional arguments:
  {register,context,work-context,runtime-proof,help}
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac binding

```console
$ mac binding --help
usage: mac binding [-h] {register,help} ...

Hermes platform bindings

positional arguments:
  {register,help}
    help           show help for this command group

options:
  -h, --help       show this help message and exit
```

## mac interaction

```console
$ mac interaction --help
usage: mac interaction [-h] {task,help} ...

durable work created from a conversation

positional arguments:
  {task,help}
    help       show help for this command group

options:
  -h, --help   show this help message and exit
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
  start
  release  clear a --no-dispatch hold so the task can auto-dispatch
  close    transition a task to completed/cancelled; cancellation requires a reason
  reopen   recovery: return a stuck/terminal task (failed/cancelled/blocked) to OPEN for retry or reconciliation

Review and evidence:
  evidence
  submit-review
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
  wait    wait for a project's tasks to finish, streaming each transition
  edit    answer a parked task in $EDITOR; saving submits it back to the queue
  select  preview the group of tasks a selector expression names
  batch   apply one operation to every task a selector names (dry by default)
  group   named, saved task groups
  egress  declare which hosts a task's sandbox may reach

Run `mac task help <subcommand>` for the arguments one takes.
```

## mac repo

```console
$ mac repo --help
usage: mac repo [-h] {refs,help} ...

repositories that tasks execute against

positional arguments:
  {refs,help}
    refs       audit or prune task-owned remote branches
    help       show help for this command group

options:
  -h, --help   show this help message and exit
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
  list    list every project
  show    show one project: policy, repositories, dispatch state
  update  update project fields or its branch-qualified repository registration
  delete  remove a project; --force detaches historical tasks and disables linked checkout registrations (same as `unregister`)

Dispatch:
  pause     hold a project's tickets from autonomous dispatch
  activate  open a project to autonomous dispatch

Repositories:
  register  register the current checkout, a local path, or GIT_URL[#BRANCH] as a project and create its contract-authoring task

Run `mac project help <subcommand>` for the arguments one takes.
```

## mac sandbox

```console
$ mac sandbox --help
usage: mac sandbox [-h] {bom,rollout,help} ...

derive, check, and roll out the OpenShell sandbox image

positional arguments:
  {bom,rollout,help}
    bom               derive the sandbox bill of materials from every
                      project's contract
    rollout           roll a reviewed sandbox image onto each worker, after it
                      drains
    help              show help for this command group

options:
  -h, --help          show this help message and exit
```

## mac work-package

```console
$ mac work-package --help
usage: mac work-package [-h] SUBCOMMAND ...

positional arguments:
  SUBCOMMAND

options:
  -h, --help  show this help message and exit

work-package -- a task group: several tasks assembled, certified and landed together

CRUD:
  create  freeze and assemble exact accepted inputs for one integration node (same as `assemble`)
  list    list work packages
  show    show one work package: member tasks, plan, certification state

Assembly:
  assemble-batch   assemble a previously-created integration batch
  assembly-claim   claim an integration batch under the controller fence
  assembly-status  show an integrity-checked integration-batch snapshot
  admit            compile, attest, and atomically materialize a held plan

Planning:
  replan          atomically install one paused package's compiled replacement plan
  replan-preview  compile and attest plan N+1, then report whether it can be applied
  readiness       show credential/capability activation blockers

Certification:
  certification-prepare        prepare an immutable external-certifier job for an exact batch
  certification-claim          claim a certification job under its monotonic controller fence
  certification-run            explicitly run and ingest a prepared OpenShell certification job
  certification-ingest         ingest one exact fenced external-certifier result
  certification-status         show an integrity-checked certification job snapshot
  accept-certification         accept one exact passed certification for landing
  reject-failed-certification  read back the atomic Andon disposition for a failed certification

Candidates:
  accept-candidate  accept one reviewed, controller-verified package candidate
  reject-candidate  reject one package candidate and stage its bounded rework decision
  verify-output     controller-observe an immutable attempt and append its receipt

Landing:
  land                  land one accepted exact candidate on its registered repository
  finalize-publication  consume the exact landing receipt and complete the product graph

Dispatch:
  pause     raise the package Andon by exact plan-version and epoch CAS
  activate

Not available for work-package: update, delete (no control-plane operation implements it)

Run `mac work-package help <subcommand>` for the arguments one takes.
```

## mac directive

```console
$ mac directive --help
usage: mac directive [-h]
                     {propose,list,show,versions,check,impact,approve,activate,deactivate,effective,binding,waiver,help} ...

operator directives issued to agents

positional arguments:
  {propose,list,show,versions,check,impact,approve,activate,deactivate,effective,binding,waiver,help}
    propose             validate and create an immutable directive version
    check               analyze exact policy, bindings, conflicts, and macro
                        effects
    approve             approve one exact passing check and immutable digest
    activate            distribute an approved version for fleet
                        acknowledgement
    effective           render the currently active policy snapshot
    binding             hub-owned variable bindings
    waiver              audited exact-version repository/project exceptions
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac hgx

```console
$ mac hgx --help
usage: mac hgx [-h] {capacity,help} ...

HGX / GPU capacity management

positional arguments:
  {capacity,help}
    capacity       plan, inspect, or explicitly create bounded standard-dind
                   capacity
    help           show help for this command group

options:
  -h, --help       show this help message and exit
```

## mac openshell

```console
$ mac openshell --help
usage: mac openshell [-h]
                     {reconcile,sandbox-gc,reap-orphans,render-policy,policy,status,help} ...

sandboxed execution environments for agents

positional arguments:
  {reconcile,sandbox-gc,reap-orphans,render-policy,policy,status,help}
    reconcile           reconcile fleet OpenShell required/policy/deployment
                        status after host validation
    sandbox-gc          list or delete old orphaned MAC-owned OpenShell
                        sandboxes
    reap-orphans        fail-closed reap of MAC-owned task sandboxes with
                        mac.keep=false and a dead recorded PID (no age wait)
    render-policy       render the OpenShell guardrail policy from the
                        operator template for this fleet
    policy              MAC-managed OpenShell policies
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac machine

```console
$ mac machine --help
usage: mac machine [-h] {register,list,show,help} ...

hosts that agents run on

positional arguments:
  {register,list,show,help}
    list                list all registered machines
    show                show full record for one machine
    help                show help for this command group

options:
  -h, --help            show this help message and exit
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
  heartbeat
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

## mac fleet

```console
$ mac fleet --help
usage: mac fleet [-h]
                 {build-distribution,target,backlog-groom,model-selection,ssh-spec,refresh-source,refresh,snapshot,soul-pull,soul-push,soul-audit,memory-export,memory-prune,refresh-context,validate,doctor,sync-token,creds-status,creds-sync,github-ingest,rotate-token,move-agent,help} ...

deploy, inspect and maintain the fleet as a whole

positional arguments:
  {build-distribution,target,backlog-groom,model-selection,ssh-spec,refresh-source,refresh,snapshot,soul-pull,soul-push,soul-audit,memory-export,memory-prune,refresh-context,validate,doctor,sync-token,creds-status,creds-sync,github-ingest,rotate-token,move-agent,help}
    build-distribution  aggregate live agents by running_digest
    target              authoritative per-role fleet version pin (source rev +
                        OpenClaw VERSION/REVISION)
    backlog-groom       autonomous backlog grooming: status, manual run, per-
                        project opt-in
    model-selection     dynamic powerhouse-model selection: status, refresh,
                        promote a pending swap
    ssh-spec            resolve the canonical secret-free SSH route for one
                        fleet agent
    refresh-source (refresh)
                        ask fleet agents to pull their self-update repo and
                        restart themselves if HEAD changes
    snapshot            compact view of the fleet: who's online + what each
                        agent is working on
    soul-pull           pull each agent's editable soul text
                        (SOUL/USER/MEMORY.md) into a local tree
    soul-push           diff an edited soul snapshot vs live and write changes
                        (backup-before-replace)
    soul-audit          audit the remote ~/.hermes directory for a named agent
    memory-export       export Qdrant vector memory to greppable JSONL for
                        vetting
    memory-prune        DELETE vetted Qdrant point ids from a collection
                        (destructive)
    refresh-context     refresh the live Fleet section in this agent's
                        runtime-context markdown (what the nap-tick-style
                        timer calls so each session knows its teammates)
    validate            validate a declarative mac.fleet_setup.v1 setup spec
    doctor              run setup doctor checks for a declarative fleet spec
    sync-token          pull the hub's current MAC_API_TOKEN into ~/.mac/.env
                        as MAC_API_TOKEN__<FLEET> (fixes 403 'unknown bearer
                        token' drift)
    creds-status        per-agent coding-CLI (claude/codex/cursor) auth
                        status, from the agents' own heartbeat reports; flags
                        who needs a credential sync
    creds-sync          push THIS workstation's coding-CLI credentials to
                        fleet workers over their SSH routes (stdin-only
                        transfer; verified on arrival)
    github-ingest       GitHub-issue work generator: status, manual run, and
                        per-project opt-in
    rotate-token        rotate the hub bearer token with an overlap window
                        (dry-run unless --apply)
    move-agent          move an agent between fleets: rewrite fleets.yaml
                        entry, print redeploy command, and emit DB reconcile
                        commands. Dry-run by default; pass --execute to mutate
                        fleets.yaml.
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac journal

```console
$ mac journal --help
usage: mac journal [-h] {snapshot,list,restore,help} ...

per-agent narrative history

positional arguments:
  {snapshot,list,restore,help}
    snapshot            snapshot SOUL/USER/MEMORY/memories/mood/config to
                        $HOME/.mac/journal/<date>/ and run
                        MAC_JOURNAL_BACKUP_HOOK if set
    list                list journaled snapshots
    restore             restore an agent's state from a journal date
                        (snapshots current state first, so it's reversible)
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac optimizer

```console
$ mac optimizer --help
usage: mac optimizer [-h] {status,tick,policy,experiment,help} ...

Create allowlisted execution policies, run controlled task experiments, and
promote only statistically superior, quality-noninferior treatments.

positional arguments:
  {status,tick,policy,experiment,help}
    status              show scheduler and active experiments
    tick                run one observation, decision, and hypothesis pass now
    policy              versioned execution-policy lifecycle
    experiment          controlled experiment lifecycle
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac mood

```console
$ mac mood --help
usage: mac mood [-h] {set,show,clear,history,help} ...

agent temperament and its effect on execution

positional arguments:
  {set,show,clear,history,help}
    set                 record a mood transition
    show                current mood for an agent
    clear               end the active overlay
    history             mood transitions for an agent
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac dream

```console
$ mac dream --help
usage: mac dream [-h] {run,list,show,promote,discard,import-logs,help} ...

offline pattern-finding over past work

positional arguments:
  {run,list,show,promote,discard,import-logs,help}
    run                 curate memory into a reviewable candidate store
    list                list dream runs, newest first
    show                show a run, its gates and candidates
    promote             adopt a reviewed run into live memory
    discard             discard a dream run
    import-logs         merge orphaned ~/.hermes/dream_logs reports into
                        durable memory
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac nap

```console
$ mac nap --help
usage: mac nap [-h]
               {configure,show,next,begin,complete,fail,list,cycle,due,consolidate,help} ...

consolidation cycles that summarize recent work

positional arguments:
  {configure,show,next,begin,complete,fail,list,cycle,due,consolidate,help}
    configure           set or refresh an agent's nap schedule (offset
                        defaults to a deterministic hash of agent.name)
    next                compute the next nap window
    begin               start a nap; transitions the agent to DRAINING
    complete            mark a nap_run completed and restore the agent
    fail                mark a nap_run failed and restore the agent
    list                list nap_runs
    cycle               run a full nap cycle (begin + consolidate + complete)
                        for one agent — what the auto-trigger timer calls
    due                 list enabled nap_schedules whose current window has
                        opened and not yet been completed
    consolidate         walk the agent's recent memory_records, summarize by
                        task/project, write a nap_summary row per group, and
                        embed into the medium tier (mem-08)
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac dispatch

```console
$ mac dispatch --help
usage: mac dispatch [-h] {assign,tick,help} ...

the loop that matches ready tasks to eligible agents

positional arguments:
  {assign,tick,help}
    help              show help for this command group

options:
  -h, --help          show this help message and exit
```

## mac message

```console
$ mac message --help
usage: mac message [-h] {send,inbox,help} ...

messages between agents and humans

positional arguments:
  {send,inbox,help}
    help             show help for this command group

options:
  -h, --help         show this help message and exit
```

## mac agentbus

```console
$ mac agentbus --help
usage: mac agentbus [-h]
                    {open,append,close,list,wait,read,publish,repo-update,artifact-publish,help} ...

the agent-to-agent message bus

positional arguments:
  {open,append,close,list,wait,read,publish,repo-update,artifact-publish,help}
    wait                block until this agent is messaged, then print and
                        exit
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac review

```console
$ mac review --help
usage: mac review [-h] {request,decision,auto-land,experiment,help} ...

adversarial review of completed work

positional arguments:
  {request,decision,auto-land,experiment,help}
    auto-land           land a task/branch iff the contract gate is GREEN and
                        an independent adversarial reviewer APPROVEs (default-
                        to-reject)
    experiment          assign and inspect replayable review-strategy
                        experiments
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac publish

```console
$ mac publish --help
usage: mac publish [-h] [--evidence-id EVIDENCE_ID] task_id target created_by

publish reviewed work to its destination

positional arguments:
  task_id
  target
  created_by

options:
  -h, --help            show this help message and exit
  --evidence-id EVIDENCE_ID
```

## mac pull-request

```console
$ mac pull-request --help
usage: mac pull-request [-h] {open,help} ...

pull requests raised from task work

positional arguments:
  {open,help}
    open       open a PR/MR on github or gitea
    help       show help for this command group

options:
  -h, --help   show this help message and exit
```

## mac secret

```console
$ mac secret --help
usage: mac secret [-h] {set,list,delete,rotate,access,audits,help} ...

secret storage, rotation and access audit

positional arguments:
  {set,list,delete,rotate,access,audits,help}
    delete              hard-delete a secret (scrub its value)
    rotate              rotate a secret's value in place (audited)
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac runtime

```console
$ mac runtime --help
usage: mac runtime [-h] {create,list,delta,help} ...

runtime images and environment definitions

positional arguments:
  {create,list,delta,help}
    delta               runtime environment delta lifecycle
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac artifact

```console
$ mac artifact --help
usage: mac artifact [-h] {register,list,show,delete,help} ...

durable artifacts produced by task work

positional arguments:
  {register,list,show,delete,help}
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac env

```console
$ mac env --help
usage: mac env [-h] {register,list,show,deploy,current,history,help} ...

environment variables projected onto fleet hosts

positional arguments:
  {register,list,show,deploy,current,history,help}
    deploy              record a new active deployment in an environment,
                        retiring the prior one
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac bridge

```console
$ mac bridge --help
usage: mac bridge [-h] {import,list,repository,help} ...

external system bridges

positional arguments:
  {import,list,repository,help}
    repository          registered project repository
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac integrations

```console
$ mac integrations --help
usage: mac integrations [-h] {findings,observations,help} ...

third-party integrations

positional arguments:
  {findings,observations,help}
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac curiosity

```console
$ mac curiosity --help
usage: mac curiosity [-h] {list,approve,reject,help} ...

quarantined self-proposed experiments awaiting judgment

positional arguments:
  {list,approve,reject,help}
    list                list candidates
    approve             approve a quarantined candidate
    reject              reject a quarantined candidate
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac memory

```console
$ mac memory --help
usage: mac memory [-h]
                  {decay,add,search,remember,list,forget,summarize-actions,embed,backfill,health,recall,recall-dreams,help} ...

durable cross-session knowledge

positional arguments:
  {decay,add,search,remember,list,forget,summarize-actions,embed,backfill,health,recall,recall-dreams,help}
    decay               dream-04: forget stale, low-salience memory records
                        (dry-run unless --apply); curated knowledge (user/proj
                        ect/feedback/deployment_learning/fleet_learning/dream/
                        beads_memory) is preserved
    remember            store an ambient project-scoped fact (bd remember
                        equivalent)
    list                list project-scoped memories (bd memories equivalent)
    forget              delete a project-scoped memory by key (bd forget
                        equivalent)
    summarize-actions   write a bounded memory record from action ledger
                        summaries
    embed               embed one memory_record into the vector tier (mem-07)
    backfill            embed every memory_record not yet in the target Qdrant
                        collection (mem-07)
    health              mem-10: memory-tier health snapshot (counts + alerts
                        for inert vector tier / stalled consolidator / disk
                        bloat)
    recall              vector-tier recall (mem-09): embed query and return
                        top ranked memory hits with their summaries
    recall-dreams       recall typed dream artifacts with
                        scope/kind/confidence filters
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac rollout

```console
$ mac rollout --help
usage: mac rollout [-h]
                   {create,list,advance,verify-artifact,health,rescue,help} ...

staged rollout of a runtime or configuration

positional arguments:
  {create,list,advance,verify-artifact,health,rescue,help}
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac events

```console
$ mac events --help
usage: mac events [-h] {list,help} ...

the unified event stream

positional arguments:
  {list,help}
    list       list events across task/rollout/eval_set/secret audit surfaces
    help       show help for this command group

options:
  -h, --help   show this help message and exit
```

## mac action-events

```console
$ mac action-events --help
usage: mac action-events [-h] {list,stream,export-otlp,help} ...

recorded agent actions

positional arguments:
  {list,stream,export-otlp,help}
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac command-audit

```console
$ mac command-audit --help
usage: mac command-audit [-h] {list,help} ...

audit of commands agents ran

positional arguments:
  {list,help}
    list       list audited command start/completion events
    help       show help for this command group

options:
  -h, --help   show this help message and exit
```

## mac observability

```console
$ mac observability --help
usage: mac observability [-h] {list,prune,help} ...

structured metrics and logs

positional arguments:
  {list,prune,help}
    list             list structured observability metrics and logs
    prune            delete observability_events older than --older-than (ISO
                     timestamp) or keep only --keep-last rows; returns the
                     number removed
    help             show help for this command group

options:
  -h, --help         show this help message and exit
```

## mac communication

```console
$ mac communication --help
usage: mac communication [-h]
                         {identity,account,representation,lease,send,deliveries,help} ...

communication channels and routing

positional arguments:
  {identity,account,representation,lease,send,deliveries,help}
    identity            manage stable human-facing identities
    account             manage channel accounts owned by identities
    representation      map internal agents/roles/projects to public
                        identities
    lease               manage singleton gateway ownership of channel accounts
    send                enqueue an idempotent OpenClaw human-facing delivery
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac notifier

```console
$ mac notifier --help
usage: mac notifier [-h] {configure,list,delete,deliver,help} ...

outbound notification channels

positional arguments:
  {configure,list,delete,deliver,help}
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac migrate

```console
$ mac migrate --help
usage: mac migrate [-h] {import,acc,help} ...

schema and data migrations

positional arguments:
  {import,acc,help}
    import           replay a JSONL stream of {record:
                     tenant|user|task|evidence|history} rows
    acc              dry-run or import an ACC SQLite database once
    help             show help for this command group

options:
  -h, --help         show this help message and exit
```

## mac workflow

```console
$ mac workflow --help
usage: mac workflow [-h] {decisions,start,help} ...

multi-step workflow definitions and runs

positional arguments:
  {decisions,start,help}
    decisions           list every human-decision (approval) gate in a
                        workflow or a live run. Pass a workflow id/slug to see
                        the definition's gates, or a run id (prefix run_) for
                        live state.
    start               start a workflow run, optionally with front-loaded
                        approval decisions so the run can advance unattended.
    help                show help for this command group

options:
  -h, --help            show this help message and exit
```

## mac eval

```console
$ mac eval --help
usage: mac eval [-h] {set,run,help} ...

evaluation runs over agent output

positional arguments:
  {set,run,help}
    set           eval set commands
    run           eval run commands
    help          show help for this command group

options:
  -h, --help      show this help message and exit
```

## mac plan

```console
$ mac plan --help
usage: mac plan [-h] {order,help} ...

planning helpers, including dependency ordering

positional arguments:
  {order,help}
    order       order files/modules by import/call topology (leaf-first or
                core-first layers)
    help        show help for this command group

options:
  -h, --help    show this help message and exit
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
