# Command-line reference

This page is generated from the current parser. Do not edit it directly.
The book uses executable `bash` blocks; reference usage is rendered as output.

## mac

```console
$ mac --help
usage: mac [-h] [--db DB] [--local-authority] [--hub-url HUB_URL]
           [--token TOKEN] [--fleet FLEET] [--profile PROFILE] [--json]
           {diagnostics,init,database,config,login,logout,client,tenant,user,persona,hermes,binding,interaction,task,repo,project,work-package,directive,hgx,openshell,machine,agent,fleet,journal,optimizer,mood,dream,nap,dispatch,message,agentbus,review,publish,pull-request,secret,runtime,artifact,env,bridge,integrations,memory,rollout,events,action-events,command-audit,observability,communication,comm,notifier,migrate,workflow,eval,plan}
           ...

Multi-agent coordinator control plane

positional arguments:
  {diagnostics,init,database,config,login,logout,client,tenant,user,persona,hermes,binding,interaction,task,repo,project,work-package,directive,hgx,openshell,machine,agent,fleet,journal,optimizer,mood,dream,nap,dispatch,message,agentbus,review,publish,pull-request,secret,runtime,artifact,env,bridge,integrations,memory,rollout,events,action-events,command-audit,observability,communication,comm,notifier,migrate,workflow,eval,plan}
    diagnostics         run read-only control-plane health checks
    init                create the control-plane schema in the --db PostgreSQL
                        store
    database            offline durable-authority maintenance and migration
    config              configuration helpers
    login               bootstrap or inspect a scoped client login over
                        verified SSH
    logout              remove a local login and optionally revoke it on the
                        hub
    client              hub enrollment principals and secure local client
                        profiles
    tenant              tenant boundary commands
    user                human user identity commands
    persona             Hermes persona and memory-scope commands
    hermes              Hermes instance commands
    binding             Hermes platform binding commands
    interaction         create durable work from Hermes conversation context
    task                task ledger commands
    repo                managed repository work-ref lifecycle commands
    project             project summary commands
    work-package        versioned work-DAG admission, readiness, and
                        controller stages
    directive           versioned fleet rules, conditional bindings, and held
                        workflow macros
    hgx                 operator controls for fungible HGX provider capacity
    openshell           OpenShell sandbox guardrail commands
    machine             machine registry commands
    agent               agent registry commands
    fleet               fleet-wide queries
    journal             snapshot / restore an agent's soul + memory state
                        (guards against soul loss)
    optimizer           autonomous scientific policy optimization
    mood                agent mood overlays (agents self-report; operators
                        query)
    dream               dream-cycle learning maintenance
    nap                 agent nap schedule and lifecycle (daily memory
                        consolidation)
    dispatch            dispatcher commands
    message             structured message bus commands
    agentbus            typed high-throughput agent-to-agent content streams
    review              review pipeline commands
    pull-request        open or inspect pull/merge requests on the task's git
                        host
    secret              secret boundary commands
    runtime             runtime boundary commands
    artifact            artifact registry: canonical record for deliverables
                        (images, packages, tarballs)
    env                 environments and deployments (artifact -> environment
                        edges)
    bridge              external project bridge commands
    integrations        integration authority observations and findings
    memory              memory and provenance commands
    rollout             rollout and rescue commands
    events              unified audit stream
    action-events       canonical MAC action event ledger
    command-audit       short-retention per-agent command log
    observability       structured metric/log observations
    communication (comm)
                        logical public identities, representation, and
                        OpenClaw delivery
    notifier            operator notification channel configuration
    migrate             one-time migration from external systems
    workflow            workflow inspection (graph definitions, runs, decision
                        gates)
    eval                evaluation sets and runs
    plan                planning helpers (topology ordering, blast radius,
                        etc.)

options:
  -h, --help            show this help message and exit
  --db DB               direct PostgreSQL control-plane authority (a
                        postgres:// DSN) for hub maintenance, standalone
                        development, tests, and migration. It is not a
                        repository ticket store or offline hub replica and
                        never synchronizes with a hub. When unset and no hub
                        is configured, mac refuses to run.
  --local-authority     enable stopped-hub maintenance against the
                        authoritative PostgreSQL database selected by --db or
                        MAC_DB. The command refuses this mode while the
                        configured hub health endpoint is reachable.
  --hub-url HUB_URL     MAC hub URL (hub mode). Falls back to $MAC_API_URL /
                        $MAC_URL / $MAC_HUB_URL, then ~/.mac/fleets.yaml for
                        --fleet.
  --token TOKEN         Bearer token for hub mode. Falls back to
                        $MAC_API_TOKEN (or $MAC_API_TOKEN__<FLEET> when
                        --fleet is set).
  --fleet FLEET         Fleet name; selects MAC_API_TOKEN__<FLEET> and
                        ~/.mac/fleets.yaml entry.
  --profile PROFILE     Secure client profile under ~/.mac/clients. Falls back
                        to $MAC_PROFILE or the active profile.
  --json                Emit JSON instead of the default human-readable text.
                        Works in any position (e.g. `mac task list --json` or
                        `mac --json task list`).
```

## mac diagnostics

```console
$ mac diagnostics --help
usage: mac diagnostics [-h] [--check CHECK]

options:
  -h, --help     show this help message and exit
  --check CHECK  run only the named check (repeatable)
```

## mac init

```console
$ mac init --help
usage: mac init [-h]

options:
  -h, --help  show this help message and exit
```

## mac database

```console
$ mac database --help
usage: mac database [-h] {migrate-sqlite-to-postgres} ...

positional arguments:
  {migrate-sqlite-to-postgres}
    migrate-sqlite-to-postgres
                        copy every SQLite row to PostgreSQL and verify full-
                        row digests

options:
  -h, --help            show this help message and exit
```

## mac config

```console
$ mac config --help
usage: mac config [-h] {migrate-env-namespace} ...

positional arguments:
  {migrate-env-namespace}
    migrate-env-namespace
                        add fleet-scoped variants of flat MAC_* credentials in
                        ~/.mac/.env (mac-g55y)

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
usage: mac client [-h] {enroll,renew,revoke,list,profile} ...

positional arguments:
  {enroll,renew,revoke,list,profile}
    enroll              hub-local: mint a revocable scoped credential (invoke
                        through SSH)
    renew               hub-local: rotate one client's token and expiry
    revoke              hub-local: immediately revoke one client credential
    list                hub-local: list client principals without token hashes
    profile             install, select, inspect, or remove local secure
                        profiles

options:
  -h, --help            show this help message and exit
```

## mac tenant

```console
$ mac tenant --help
usage: mac tenant [-h] {register,list} ...

positional arguments:
  {register,list}

options:
  -h, --help       show this help message and exit
```

## mac user

```console
$ mac user --help
usage: mac user [-h] {register} ...

positional arguments:
  {register}

options:
  -h, --help  show this help message and exit
```

## mac persona

```console
$ mac persona --help
usage: mac persona [-h] {register} ...

positional arguments:
  {register}

options:
  -h, --help  show this help message and exit
```

## mac hermes

```console
$ mac hermes --help
usage: mac hermes [-h] {register,context,work-context,runtime-proof} ...

positional arguments:
  {register,context,work-context,runtime-proof}

options:
  -h, --help            show this help message and exit
```

## mac binding

```console
$ mac binding --help
usage: mac binding [-h] {register} ...

positional arguments:
  {register}

options:
  -h, --help  show this help message and exit
```

## mac interaction

```console
$ mac interaction --help
usage: mac interaction [-h] {task} ...

positional arguments:
  {task}

options:
  -h, --help  show this help message and exit
```

## mac task

```console
$ mac task --help
usage: mac task [-h]
                {create,list,show,summary,ready,why-unclaimed,claim,break-glass,break-glass-list,break-glass-revoke,close,cancel,ask,needs-input,edit,answer,reopen,recover-stranded,recover-finalizer,recover-stalled-finalizer,force-complete,search,stats,generator-yield,throughput,audit,start,release,submit-review,evidence,detect-beads,migrate-beads,detect-ticketing,convert-ticketing}
                ...

positional arguments:
  {create,list,show,summary,ready,why-unclaimed,claim,break-glass,break-glass-list,break-glass-revoke,close,cancel,ask,needs-input,edit,answer,reopen,recover-stranded,recover-finalizer,recover-stalled-finalizer,force-complete,search,stats,generator-yield,throughput,audit,start,release,submit-review,evidence,detect-beads,migrate-beads,detect-ticketing,convert-ticketing}
    list                list tasks (default: short ids; use --full-ids for
                        scripts)
    show                show task state, activity, diagnoses, and compact
                        evidence counts (use --json for the complete
                        structured record)
    summary             activity-only task narrative (also included in `task
                        show`)
    ready               list task-ready work and the number of currently
                        eligible fleet agents
    why-unclaimed       show the authoritative task and agent reasons
                        preventing a claim
    claim               atomically claim a task for an agent
    break-glass         admin-only: authorize an exact task/agent pair for
                        single-use direct host execution
    break-glass-list    list durable break-glass authorizations for a task
    break-glass-revoke  admin-only: revoke an unclaimed host authorization
    close               transition a task to completed/cancelled; cancellation
                        requires a reason
    cancel              actively cancel a task, revoke its lease, and abort a
                        running worker executor
    ask                 park a task on an unanswered human question (not a
                        failure; never reaped)
    needs-input         list tasks parked on an unanswered human question (the
                        operator inbox)
    edit                answer a parked task in $EDITOR; saving submits it
                        back to the queue
    answer              answer a parked task's question and return it to the
                        dispatch pool
    reopen              recovery: return a stuck/terminal task
                        (failed/cancelled/blocked) to OPEN for retry or
                        reconciliation
    recover-stranded    re-supervise tasks left waiting on a terminal
                        dependency (dry-run unless --apply)
    recover-finalizer   revalidate and publish preserved work refused for
                        uncommitted new files
    recover-stalled-finalizer
                        resume a deterministic finalizer that stalled
                        (timeout/cancel/crash) after harvesting verified work
    force-complete      BREAK-GLASS operator override: mark a task COMPLETED
                        regardless of state/review (bypasses the adversarial
                        auto-land gate; audited). Not the normal path — the
                        adversarial reviewer + contract gate auto-land is.
    search              keyword search across task title and description
    stats               count tasks by state
    generator-yield     show each task origin's completion yield and whether
                        the yield gate is letting it file
    throughput          task-to-main KPIs, stage dwell, stranded work, and
                        resource collisions
    audit               read-only reconciliation of every task's history,
                        evidence, dependencies, replacements, and git ancestry
    release             clear a --no-dispatch hold so the task can auto-
                        dispatch
    detect-beads        inspect a repo for .beads/ artifacts (read-only)
    migrate-beads       import .beads/issues.jsonl into MAC tasks and emit
                        .tickets/<id>.md
    detect-ticketing    detect ticketing sources in a repo (.tickets local
                        mirror / .beads foreign) and whether a one-way
                        conversion should be offered (read-only)
    convert-ticketing   one-way convert a detected foreign source (e.g. beads)
                        into MAC ledger tasks plus optional local
                        compatibility files (run after the user agrees)

options:
  -h, --help            show this help message and exit
```

## mac repo

```console
$ mac repo --help
usage: mac repo [-h] {refs} ...

positional arguments:
  {refs}
    refs      audit or prune task-owned remote branches

options:
  -h, --help  show this help message and exit
```

## mac project

```console
$ mac project --help
usage: mac project [-h]
                   {create,register,pause,activate,list,show,update,unregister}
                   ...

positional arguments:
  {create,register,pause,activate,list,show,update,unregister}
    register            register the current checkout, a local path, or
                        GIT_URL[#BRANCH] as a project and create its contract-
                        authoring task
    pause               hold a project's tickets from autonomous dispatch
    activate            open a project to autonomous dispatch
    update              update project fields or its branch-qualified
                        repository registration
    unregister          remove a project; --force detaches historical tasks
                        and disables linked checkout registrations

options:
  -h, --help            show this help message and exit
```

## mac work-package

```console
$ mac work-package --help
usage: mac work-package [-h]
                        {admit,list,show,readiness,activate,replan-preview,pause,replan,verify-output,accept-candidate,reject-candidate,assemble,assembly-status,assembly-claim,assemble-batch,certification-prepare,certification-status,certification-claim,certification-ingest,certification-run,reject-failed-certification,accept-certification,land,finalize-publication}
                        ...

positional arguments:
  {admit,list,show,readiness,activate,replan-preview,pause,replan,verify-output,accept-candidate,reject-candidate,assemble,assembly-status,assembly-claim,assemble-batch,certification-prepare,certification-status,certification-claim,certification-ingest,certification-run,reject-failed-certification,accept-certification,land,finalize-publication}
    admit               compile, attest, and atomically materialize a held
                        plan
    readiness           show credential/capability activation blockers
    replan-preview      compile and attest plan N+1, then report whether it
                        can be applied
    pause               raise the package Andon by exact plan-version and
                        epoch CAS
    replan              atomically install one paused package's compiled
                        replacement plan
    verify-output       controller-observe an immutable attempt and append its
                        receipt
    accept-candidate    accept one reviewed, controller-verified package
                        candidate
    reject-candidate    reject one package candidate and stage its bounded
                        rework decision
    assemble            freeze and assemble exact accepted inputs for one
                        integration node
    assembly-status     show an integrity-checked integration-batch snapshot
    assembly-claim      claim an integration batch under the controller fence
    assemble-batch      assemble a previously-created integration batch
    certification-prepare
                        prepare an immutable external-certifier job for an
                        exact batch
    certification-status
                        show an integrity-checked certification job snapshot
    certification-claim
                        claim a certification job under its monotonic
                        controller fence
    certification-ingest
                        ingest one exact fenced external-certifier result
    certification-run   explicitly run and ingest a prepared OpenShell
                        certification job
    reject-failed-certification
                        read back the atomic Andon disposition for a failed
                        certification
    accept-certification
                        accept one exact passed certification for landing
    land                land one accepted exact candidate on its registered
                        repository
    finalize-publication
                        consume the exact landing receipt and complete the
                        product graph

options:
  -h, --help            show this help message and exit
```

## mac directive

```console
$ mac directive --help
usage: mac directive [-h]
                     {propose,list,show,versions,check,impact,approve,activate,deactivate,effective,binding,waiver}
                     ...

positional arguments:
  {propose,list,show,versions,check,impact,approve,activate,deactivate,effective,binding,waiver}
    propose             validate and create an immutable directive version
    check               analyze exact policy, bindings, conflicts, and macro
                        effects
    approve             approve one exact passing check and immutable digest
    activate            distribute an approved version for fleet
                        acknowledgement
    effective           render the currently active policy snapshot
    binding             hub-owned variable bindings
    waiver              audited exact-version repository/project exceptions

options:
  -h, --help            show this help message and exit
```

## mac hgx

```console
$ mac hgx --help
usage: mac hgx [-h] {capacity} ...

positional arguments:
  {capacity}
    capacity  plan, inspect, or explicitly create bounded standard-dind
              capacity

options:
  -h, --help  show this help message and exit
```

## mac openshell

```console
$ mac openshell --help
usage: mac openshell [-h]
                     {reconcile,sandbox-gc,reap-orphans,render-policy,policy,status}
                     ...

positional arguments:
  {reconcile,sandbox-gc,reap-orphans,render-policy,policy,status}
    reconcile           reconcile fleet OpenShell required/policy/deployment
                        status after host validation
    sandbox-gc          list or delete old orphaned MAC-owned OpenShell
                        sandboxes
    reap-orphans        fail-closed reap of MAC-owned task sandboxes with
                        mac.keep=false and a dead recorded PID (no age wait)
    render-policy       render the OpenShell guardrail policy from the
                        operator template for this fleet
    policy              MAC-managed OpenShell policies

options:
  -h, --help            show this help message and exit
```

## mac machine

```console
$ mac machine --help
usage: mac machine [-h] {register,list,show} ...

positional arguments:
  {register,list,show}
    list                list all registered machines
    show                show full record for one machine

options:
  -h, --help            show this help message and exit
```

## mac agent

```console
$ mac agent --help
usage: mac agent [-h]
                 {register,update,list,attestation-recover,report-executor-approve,report-executor-revoke,reflect,hardware,heartbeat,tell,deregister,delete,hold,resume,config,migrate}
                 ...

positional arguments:
  {register,update,list,attestation-recover,report-executor-approve,report-executor-revoke,reflect,hardware,heartbeat,tell,deregister,delete,hold,resume,config,migrate}
    attestation-recover
                        admin-only conditional recovery for a missing/stale
                        worker signing key
    report-executor-approve
                        approve the exact current startup-attested OpenShell
                        report executor
    report-executor-revoke
                        revoke report-repository dispatch eligibility for an
                        agent
    reflect             publish an agent's runtime self-description over
                        AgentBus
    hardware            fleet hardware inventory from self-reported
                        resources.hardware
    tell                send a hub-verified HUMAN directive to any agent over
                        AgentBus — works for Slack-less agents (GKE runners,
                        ephemeral sessions); the receiver can trust its
                        operator provenance by construction
    deregister          graceful exit for a session/ephemeral agent:
                        optionally leave one final human-facing message
                        (delivered after the agent is gone), then tombstone
                        with history preserved
    delete              decommission (tombstone) an agent: strips operational
                        overlays (moods/naps/config flags/deploy config) but
                        preserves its AgentBus streams, events, deliveries,
                        and task history
    hold                place a dispatch hold on an agent; held agents are
                        skipped during claim-next
    resume              remove the dispatch hold from an agent, making it
                        eligible for dispatch again
    config              consolidated per-agent configuration (the 'geek
                        knobs')
    migrate             move an agent (soul + memory) to a new host; dry-run
                        unless --execute

options:
  -h, --help            show this help message and exit
```

## mac fleet

```console
$ mac fleet --help
usage: mac fleet [-h]
                 {build-distribution,target,backlog-groom,model-selection,ssh-spec,refresh-source,refresh,snapshot,soul-pull,soul-push,soul-audit,memory-export,memory-prune,refresh-context,validate,doctor,sync-token,creds-status,creds-sync,github-ingest,rotate-token,move-agent}
                 ...

positional arguments:
  {build-distribution,target,backlog-groom,model-selection,ssh-spec,refresh-source,refresh,snapshot,soul-pull,soul-push,soul-audit,memory-export,memory-prune,refresh-context,validate,doctor,sync-token,creds-status,creds-sync,github-ingest,rotate-token,move-agent}
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

options:
  -h, --help            show this help message and exit
```

## mac journal

```console
$ mac journal --help
usage: mac journal [-h] {snapshot,list,restore} ...

positional arguments:
  {snapshot,list,restore}
    snapshot            snapshot SOUL/USER/MEMORY/memories/mood/config to
                        $HOME/.mac/journal/<date>/ and run
                        MAC_JOURNAL_BACKUP_HOOK if set
    list                list journaled snapshots
    restore             restore an agent's state from a journal date
                        (snapshots current state first, so it's reversible)

options:
  -h, --help            show this help message and exit
```

## mac optimizer

```console
$ mac optimizer --help
usage: mac optimizer [-h] {status,tick,policy,experiment} ...

Create allowlisted execution policies, run controlled task experiments, and
promote only statistically superior, quality-noninferior treatments.

positional arguments:
  {status,tick,policy,experiment}
    status              show scheduler and active experiments
    tick                run one observation, decision, and hypothesis pass now
    policy              versioned execution-policy lifecycle
    experiment          controlled experiment lifecycle

options:
  -h, --help            show this help message and exit
```

## mac mood

```console
$ mac mood --help
usage: mac mood [-h] {set,show,clear,history} ...

positional arguments:
  {set,show,clear,history}
    set                 record a mood transition
    show                current mood for an agent
    clear               end the active overlay
    history             mood transitions for an agent

options:
  -h, --help            show this help message and exit
```

## mac dream

```console
$ mac dream --help
usage: mac dream [-h] {run,list,show,promote,discard,import-logs} ...

positional arguments:
  {run,list,show,promote,discard,import-logs}
    run                 curate memory into a reviewable candidate store
    list                list dream runs, newest first
    show                show a run, its gates and candidates
    promote             adopt a reviewed run into live memory
    discard             discard a dream run
    import-logs         merge orphaned ~/.hermes/dream_logs reports into
                        durable memory

options:
  -h, --help            show this help message and exit
```

## mac nap

```console
$ mac nap --help
usage: mac nap [-h]
               {configure,show,next,begin,complete,fail,list,cycle,due,consolidate}
               ...

positional arguments:
  {configure,show,next,begin,complete,fail,list,cycle,due,consolidate}
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

options:
  -h, --help            show this help message and exit
```

## mac dispatch

```console
$ mac dispatch --help
usage: mac dispatch [-h] {assign,tick} ...

positional arguments:
  {assign,tick}

options:
  -h, --help     show this help message and exit
```

## mac message

```console
$ mac message --help
usage: mac message [-h] {send,inbox} ...

positional arguments:
  {send,inbox}

options:
  -h, --help    show this help message and exit
```

## mac agentbus

```console
$ mac agentbus --help
usage: mac agentbus [-h]
                    {open,append,close,list,read,publish,repo-update,artifact-publish}
                    ...

positional arguments:
  {open,append,close,list,read,publish,repo-update,artifact-publish}

options:
  -h, --help            show this help message and exit
```

## mac review

```console
$ mac review --help
usage: mac review [-h] {request,decision,auto-land,experiment} ...

positional arguments:
  {request,decision,auto-land,experiment}
    auto-land           land a task/branch iff the contract gate is GREEN and
                        an independent adversarial reviewer APPROVEs (default-
                        to-reject)
    experiment          assign and inspect replayable review-strategy
                        experiments

options:
  -h, --help            show this help message and exit
```

## mac publish

```console
$ mac publish --help
usage: mac publish [-h] [--evidence-id EVIDENCE_ID] task_id target created_by

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
usage: mac pull-request [-h] {open} ...

positional arguments:
  {open}
    open      open a PR/MR on github or gitea

options:
  -h, --help  show this help message and exit
```

## mac secret

```console
$ mac secret --help
usage: mac secret [-h] {set,list,delete,rotate,access,audits} ...

positional arguments:
  {set,list,delete,rotate,access,audits}
    delete              hard-delete a secret (scrub its value)
    rotate              rotate a secret's value in place (audited)

options:
  -h, --help            show this help message and exit
```

## mac runtime

```console
$ mac runtime --help
usage: mac runtime [-h] {create,list,delta} ...

positional arguments:
  {create,list,delta}
    delta              runtime environment delta lifecycle

options:
  -h, --help           show this help message and exit
```

## mac artifact

```console
$ mac artifact --help
usage: mac artifact [-h] {register,list,show,delete} ...

positional arguments:
  {register,list,show,delete}

options:
  -h, --help            show this help message and exit
```

## mac env

```console
$ mac env --help
usage: mac env [-h] {register,list,show,deploy,current,history} ...

positional arguments:
  {register,list,show,deploy,current,history}
    deploy              record a new active deployment in an environment,
                        retiring the prior one

options:
  -h, --help            show this help message and exit
```

## mac bridge

```console
$ mac bridge --help
usage: mac bridge [-h] {import,list,repository} ...

positional arguments:
  {import,list,repository}
    repository          registered project repository

options:
  -h, --help            show this help message and exit
```

## mac integrations

```console
$ mac integrations --help
usage: mac integrations [-h] {findings,observations} ...

positional arguments:
  {findings,observations}

options:
  -h, --help            show this help message and exit
```

## mac memory

```console
$ mac memory --help
usage: mac memory [-h]
                  {decay,add,search,remember,list,forget,summarize-actions,embed,backfill,health,recall,recall-dreams}
                  ...

positional arguments:
  {decay,add,search,remember,list,forget,summarize-actions,embed,backfill,health,recall,recall-dreams}
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

options:
  -h, --help            show this help message and exit
```

## mac rollout

```console
$ mac rollout --help
usage: mac rollout [-h]
                   {create,list,advance,verify-artifact,health,rescue} ...

positional arguments:
  {create,list,advance,verify-artifact,health,rescue}

options:
  -h, --help            show this help message and exit
```

## mac events

```console
$ mac events --help
usage: mac events [-h] {list} ...

positional arguments:
  {list}
    list      list events across task/rollout/eval_set/secret audit surfaces

options:
  -h, --help  show this help message and exit
```

## mac action-events

```console
$ mac action-events --help
usage: mac action-events [-h] {list,stream,export-otlp} ...

positional arguments:
  {list,stream,export-otlp}

options:
  -h, --help            show this help message and exit
```

## mac command-audit

```console
$ mac command-audit --help
usage: mac command-audit [-h] {list} ...

positional arguments:
  {list}
    list      list audited command start/completion events

options:
  -h, --help  show this help message and exit
```

## mac observability

```console
$ mac observability --help
usage: mac observability [-h] {list,prune} ...

positional arguments:
  {list,prune}
    list        list structured observability metrics and logs
    prune       delete observability_events older than --older-than (ISO
                timestamp) or keep only --keep-last rows; returns the number
                removed

options:
  -h, --help    show this help message and exit
```

## mac communication

```console
$ mac communication --help
usage: mac communication [-h]
                         {identity,account,representation,lease,send,deliveries}
                         ...

positional arguments:
  {identity,account,representation,lease,send,deliveries}
    identity            manage stable human-facing identities
    account             manage channel accounts owned by identities
    representation      map internal agents/roles/projects to public
                        identities
    lease               manage singleton gateway ownership of channel accounts
    send                enqueue an idempotent OpenClaw human-facing delivery

options:
  -h, --help            show this help message and exit
```

## mac comm

```console
$ mac comm --help
usage: mac communication [-h]
                         {identity,account,representation,lease,send,deliveries}
                         ...

positional arguments:
  {identity,account,representation,lease,send,deliveries}
    identity            manage stable human-facing identities
    account             manage channel accounts owned by identities
    representation      map internal agents/roles/projects to public
                        identities
    lease               manage singleton gateway ownership of channel accounts
    send                enqueue an idempotent OpenClaw human-facing delivery

options:
  -h, --help            show this help message and exit
```

## mac notifier

```console
$ mac notifier --help
usage: mac notifier [-h] {configure,list,delete,deliver} ...

positional arguments:
  {configure,list,delete,deliver}

options:
  -h, --help            show this help message and exit
```

## mac migrate

```console
$ mac migrate --help
usage: mac migrate [-h] {import,acc} ...

positional arguments:
  {import,acc}
    import      replay a JSONL stream of {record:
                tenant|user|task|evidence|history} rows
    acc         dry-run or import an ACC SQLite database once

options:
  -h, --help    show this help message and exit
```

## mac workflow

```console
$ mac workflow --help
usage: mac workflow [-h] {decisions,start} ...

positional arguments:
  {decisions,start}
    decisions        list every human-decision (approval) gate in a workflow
                     or a live run. Pass a workflow id/slug to see the
                     definition's gates, or a run id (prefix run_) for live
                     state.
    start            start a workflow run, optionally with front-loaded
                     approval decisions so the run can advance unattended.

options:
  -h, --help         show this help message and exit
```

## mac eval

```console
$ mac eval --help
usage: mac eval [-h] {set,run} ...

positional arguments:
  {set,run}
    set       eval set commands
    run       eval run commands

options:
  -h, --help  show this help message and exit
```

## mac plan

```console
$ mac plan --help
usage: mac plan [-h] {order} ...

positional arguments:
  {order}
    order     order files/modules by import/call topology (leaf-first or core-
              first layers)

options:
  -h, --help  show this help message and exit
```
