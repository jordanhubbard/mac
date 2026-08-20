# MAC Quickstart

This guide assumes no prior context about MAC, Hermes, AI agents, distributed
systems, or this repository.

## What MAC Is

MAC is a control plane for a group of AI agents.

An AI agent is a program that can talk to people, read instructions, use tools,
and perform work. A fleet is a group of agents. A control plane is the part of a
system that keeps track of what exists, what should happen next, who is doing
what, what finished, what failed, and what evidence proves it.

MAC is trying to make agent work durable and auditable. Instead of asking one
chat session to remember everything, MAC records tasks, projects, claims,
reviews, evidence, deployments, events, and operational state in one place.
Agents can restart, move between machines, or fail without losing the official
record of the work.

## The Short Story

The human talks to Hermes. Hermes has the personality, conversation memory,
skills, and Slack or Telegram connection.

Hermes creates or inspects work in MAC. MAC owns the operational truth: projects,
tasks, agents, leases, reviews, publications, rollouts, secrets, and audit
history.

Agents work through MAC. They claim tasks, start them, produce evidence, request
review, publish or merge the result, and report status back through configured
notification channels.

## The Mental Model

Think of MAC as a project office for AI workers:

- A project is an area of work, usually backed by a repository.
- A task is a specific piece of work.
- A child task is a smaller task that blocks its parent until it is done.
- An agent is a worker that can claim and execute tasks.
- A hub is the central node that runs the MAC API and shared services.
- A fleet is the hub plus the agents connected to it.
- Evidence is proof that work happened, such as test results, review notes, or a
  pushed git commit.
- Review and publication are the gates that keep unfinished branch work from
  being mistaken for completed work.

## Words You Will See

- Human: the person using the system.
- Tenant: an isolated organization or personal deployment.
- User: a human identity inside a tenant.
- Persona: a Hermes personality and memory scope.
- Hermes instance: a running named Hermes identity, such as `worker-1`.
- Platform binding: a Slack channel, Telegram chat, Discord channel, or similar
  place where Hermes talks to humans.
- Fleet: a set of MAC machines and agents managed together.
- Hub: the machine that runs the MAC API and shared services for a fleet.
- Machine: a physical or virtual computer in the fleet.
- Agent: a worker process registered with MAC.
- Project: a named area of work.
- Repository: a source checkout an agent can work in.
- Legacy Beads repository: a repository with old `bd` issues that can be
  inspected or one-time migrated into MAC. Beads is not the normal issue
  authority for new work.
- Project item: imported external work.
- Task: a durable unit of work in MAC.
- Epic or story: human-scale task groupings; in MAC they are represented by tasks
  and task relationships.
- Dependency: one task must wait for another task.
- Claim or lease: an agent's temporary right to work on a task.
- Evidence: structured proof attached to a task.
- Review: an independent check before work is accepted.
- Publication: the record that accepted work was merged, deployed, or otherwise
  delivered.
- Runtime: the code and configuration an agent is running.
- Artifact: a build output, package, image, or other deliverable.
- Environment: a place where an artifact is deployed, such as staging or
  production.
- Rollout: the controlled movement of a version through environments.
- Eval: a measured check used to prevent regressions.
- Secret: a credential or token, resolved through the in-mac LLM router in fleet deployments (the standalone TokenHub is retired).
- Qdrant: shared vector memory service for recall across agents.
- Firecrawl: web research service used by agents for search, scrape, and crawl.
- AgentBus: typed agent-to-agent content streams.
- Notifier: Slack, Telegram, or another channel that receives task progress
  events.
- Observability event: a durable event, metric, or log record used to understand
  what the system did.

## Try MAC On One Computer

Start here before deploying a real fleet.

Prerequisites:

- A terminal: a text window where you run commands.
- Python 3.11 or newer for setup orchestration.
- This repository checked out locally.
- `uv` installed, or a Python environment that can install the dependencies.

From the repository root:

```console
cd ~/Src/mac
python3 scripts/bootstrap-project.py
PATH=.venv/bin:$PATH .venv/bin/python -m pytest
```

Create a local secret key. MAC uses this to protect secret records. Keep the
value private.

```console
export MAC_SECRET_KEY="$(openssl rand -base64 32)"
```

Create a local database. `--db` takes a PostgreSQL DSN; a disposable one for
this tutorial comes from `scripts/start-test-postgres.sh`:

```console
eval "$(scripts/start-test-postgres.sh)"
export TUTORIAL_DB="$MAC_TEST_PG_URL"
uv run mac --db "$TUTORIAL_DB" admin init
```

This creates a standalone control-plane authority, not an offline replica of a
fleet hub. Tasks written here can be dispatched only by an API, dispatcher, and
workers configured to use this same database. They are never synchronized with
a remote hub. A disposable database is appropriate for this tutorial. `MAC_DB`
configures a server and is not an implicit CLI selector; continue passing
`--db "$TUTORIAL_DB"` for this standalone tutorial. Direct access to a deployed
hub's configured database requires `--local-authority`, and the hub must be
stopped first.

If this machine is becoming a client of a remote fleet, a standalone database
like this one is simply discarded — there is no transfer path from it into a
hub. Create the work you want the fleet to run against the hub itself, after
`mac admin login`.

Create a practice project and task:

```console
uv run mac --db "$TUTORIAL_DB" project create demo \
  --description "A safe local project for learning MAC" \
  --active

uv run mac --db "$TUTORIAL_DB" task create "Write a hello-world note" \
  --project demo \
  --description "Create a tiny file and record what was done." \
  --required-capabilities docs

uv run mac --db "$TUTORIAL_DB" task list
```

At this point MAC has durable state: a project and a task. No agent has done the
task yet. You have created the official work record.

`mac task ready` shows open tasks that have no unfinished dependencies, no
owner/lease, no per-task dispatch hold, and no project-level dispatch pause. A
staged task is represented by `metadata.no_dispatch: true`:

```console
uv run mac --db "$TUTORIAL_DB" task create "Stage work for later" \
  --project demo \
  --description "Do not let the fleet auto-claim this yet." \
  --no-dispatch

uv run mac --db "$TUTORIAL_DB" task release task_...
```

`task release` clears the `no_dispatch` metadata key; it does not store
`no_dispatch: false`. Once the key is absent, the task is dispatchable again,
subject to dependencies, worker capability match, and project dispatch state.

## Tell Agents To Work On A Project

MAC agents do not watch arbitrary repositories. They work from dispatchable
tasks in the hub ledger. The operator flow is:

```console
# Repository-backed project: let MAC clone/analyze the repo and create the
# onboarding task that authors .mac/project.yaml.
uv run mac --db "$TUTORIAL_DB" project register git@github.com:ORG/REPO.git#main --project my-project

# After onboarding, register the hub-visible checkout that contains the contract.
uv run mac --db "$TUTORIAL_DB" admin bridge repository register my-project /srv/repos/my-project --project my-project

Registration validates `.mac/project.yaml` and, when CodeGraph is installed on
the registering host, initializes a local CodeGraph index for the checkout.
The generated `.codegraph/` directory is excluded through `.git/info/exclude`
and is not part of the repository contract or task deliverables.
Deployed fleet agents may rely on CodeGraph as a baseline analysis aid for
understanding APIs, code behavior, call relationships, and code-aware skills,
while still verifying findings against source files and tests.

# Or create a manual project. New projects default to paused, so pass --active
# when the fleet should be allowed to claim its tasks immediately.
uv run mac --db "$TUTORIAL_DB" project create my-project --active

uv run mac --db "$TUTORIAL_DB" task create "Fix failing tests" \
  --project my-project \
  --description-file desc.txt \
  --required-capabilities python

# If the project was staged earlier, open the project-level gate.
uv run mac --db "$TUTORIAL_DB" project activate my-project

# If an individual task was staged with --no-dispatch, open the task-level gate.
uv run mac --db "$TUTORIAL_DB" task release task_...

# Ask the dispatcher to assign ready work immediately.
uv run mac --db "$TUTORIAL_DB" admin dispatch tick --limit 10
```

Loop-mode agents can now claim matching work from `mac task ready`. To assign a
specific task to a specific agent manually, use:

```console
uv run mac --db "$TUTORIAL_DB" task claim task_... agent_...
uv run mac --db "$TUTORIAL_DB" task start task_... agent_...
```

## How Reviewers Learn Repository Access

For a repository-backed task, MAC does not pick every healthy reviewer as if
its Git credentials were interchangeable. A review clone writes a shared,
secret-free `fleet_learning:repository_access` record containing the project,
repository host, operation, agent, credential source *name*, outcome, and a
redacted failure class. Credential values and authenticated URLs are never
stored in memory or task evidence.

Reviewer routing uses the newest matching record:

- a recent successful `review_clone` is preferred;
- a newer authentication or authorization failure temporarily excludes that
  agent for the same project and repository host;
- a later success immediately supersedes the failure;
- an expired failure cooldown returns the agent to unknown/eligible rather
  than banning it permanently.

Inspect the records in the local demo database below. For a configured fleet,
omit `--db "$TUTORIAL_DB"` and use the selected hub profile (for example
`mac --fleet my-fleet --json memory search ...`):

```console
uv run mac --db "$TUTORIAL_DB" --json admin memory search \
  --record-type fleet_learning:repository_access \
  --order desc --limit 50

uv run mac --db "$TUTORIAL_DB" --json admin memory search \
  --subject-type agent --subject-id agent_... \
  --record-type fleet_learning:repository_access \
  --order desc --limit 20
```

Repository access and review verdict production are separate stages. A
successful clone proves only that the reviewer can read that repository. If
the review executor later fails to create signed `review_verdict` evidence,
the review is retracted after the delivered-nudge cap and the task remains
visible for repair; MAC does not reinterpret that executor failure as a Git
credential failure.

## Connect A New Client Today

Use `mac admin login` when a new machine has the MAC CLI plus key-based SSH access to
the hub. Supply a private identity and a verified known-hosts file (or a pinned
`SHA256:` host fingerprint for a directly reachable hub):

```console
mac admin login --ssh mac@hub.internal \
  --identity-file ~/.ssh/mac-my-fleet \
  --known-hosts-file ~/.ssh/mac-my-fleet-known-hosts \
  --fleet my-fleet --profile my-fleet --client-id my-laptop

mac admin login status --profile my-fleet
mac admin diagnostics
mac task stats
mac agent list
```

The active profile is selected automatically. If the managed SSH process exits,
the next profile-backed command starts a fresh verified tunnel and validates the
stored credential before dispatch. Inspect without restarting it using `mac
login status`; rotate with `mac login renew`; revoke remotely before removing
local state with `mac admin logout --revoke`.

For a bastion route, put the hub and jump-host keys in the supplied known-hosts
file and add `--proxy-jump user@bastion`. Fingerprint discovery deliberately
does not traverse a proxy jump. See [SSH Client Bootstrap
Contracts](client-bootstrap-contract.md) for the lower-level recovery workflow,
file modes, and failure semantics.

For a directly reachable hub, a scoped API token can still be provisioned out
of band:

```console
export MAC_API_URL=https://mac.example.internal
export MAC_API_TOKEN=<scoped-client-token>

mac admin diagnostics
mac task stats
mac agent list
```

If the client already has a home-scoped `~/.mac/fleets.yaml` entry with a
verified SSH route to the hub, it can refresh the fleet-scoped token and use
the legacy fleet selector:

```console
mac admin fleet sync-token --fleet my-fleet
mac --fleet my-fleet diagnostics
mac --fleet my-fleet task stats
```

`mac admin fleet sync-token` copies the historical shared administrator token. Treat
it as existing-operator recovery, not new-client enrollment. Do not copy
database credentials, `MAC_SECRET_KEY`, provider keys, hub/spoke private keys, or a different
operator's complete `~/.mac` directory. New clients should use the scoped SSH
enrollment and mode-`0600` profile credential above.

## Run The API And Dashboard

Start the API:

```console
MAC_SECRET_KEY="$MAC_SECRET_KEY" MAC_DB="$TUTORIAL_DB" \
  uv run uvicorn mac.api:app --reload --port 8789
```

Open the dashboard:

```text
http://127.0.0.1:8789/ui
```

In another terminal, inspect the same state through the hub CLI:

```console
mac --hub-url http://127.0.0.1:8789 project list
mac --hub-url http://127.0.0.1:8789 task list
```

If you configured `MAC_API_TOKEN`, pass it with `--token`. With no API token
configured, the local development API is open on localhost. (The legacy `hgmac`
binary is gone — all of its functionality lives under `mac` now.)

## Split A Large Task

If an agent claims a task and decides it is too large, it should add child tasks
instead of trying to finish everything in one step. The parent task becomes
blocked until the children complete.

Child tasks are added via the API (`POST /tasks/{id}/children`) — an executing
agent typically emits a `plan_steps` list in its evidence and the executor
posts the children automatically (auto-decompose); the Hermes adapter exposes
`mac-hermes add-child-task` for the same. For example, over the API:

```console
curl -sX POST http://127.0.0.1:8789/tasks/task_.../children \
  -H 'Content-Type: application/json' \
  -d '{"children":[{"title":"Write the first draft","description":"Produce the first small deliverable."}]}'
```

This is the same idea used in systems such as Jira: large work is represented by
relationships between tasks, not by one vague task with hidden subtasks in a
chat transcript.

## What A Real Agent Does

In normal operation an agent follows this loop:

1. Register with MAC.
2. Heartbeat so the hub knows it is alive.
3. Ask MAC for a task it is allowed to claim.
4. Start the task, which sets `started_at`.
5. Work in a task-owned checkout or workspace.
6. Record command audit, evidence, and status updates.
7. Ask for review.
8. Publish or merge only after the review gate passes.
9. Complete the task, which sets `completed_at`.

MAC also keeps `last_updated_at` so humans can see whether a task is moving or
stale.

## How Hermes Fits In

Hermes is the human-facing agent runtime. It owns:

- Conversation.
- Personality.
- Skills.
- Slack, Telegram, Discord, CLI, and similar gateways.
- Personal memory and soul files.

MAC owns:

- Durable projects and tasks.
- Agent identity and leases.
- Reviews, evidence, publications, and audit trails.
- Fleet topology and runtime state.
- Operational memory, not private personality memory.

The bridge between them is `mac-hermes`. Hermes can use it to create tasks,
list projects, claim work, add child tasks, record evidence, run web research,
and write completed operational context back to MAC.

## Deploy A Real Fleet

After the local quickstart makes sense, deploy a hub. The fleet registry is
home-scoped at `~/.mac/fleets.yaml`; it is not checked into the repository.

For an LLM-driven setup, start from a generic, per-CSP sample instead of writing
a `mac.fleet_setup.v1` spec from scratch. The repo ships de-personalized samples
under `deploy/fleet/samples/` (GKE is the worked example); your real, named fleet
spec lives outside git in `~/.mac/specs/<fleet>.fleet.yaml`:

```console
scripts/setup-fleet.py --list-samples                  # browse per-CSP samples
scripts/setup-fleet.py --init-from gke --name my-gke   # -> ~/.mac/specs/my-gke.fleet.yaml
$EDITOR ~/.mac/specs/my-gke.fleet.yaml                 # fill in the <placeholders>

mac admin fleet validate --spec ~/.mac/specs/my-gke.fleet.yaml
mac admin fleet doctor --spec ~/.mac/specs/my-gke.fleet.yaml
make setup ARGS="--spec ~/.mac/specs/my-gke.fleet.yaml --force"
```

See `deploy/fleet/samples/README.md` for the per-CSP convention. Never check a
named fleet into the repo.

The doctor report is JSON and calls out missing provider env vars, bad targets,
sample-config mistakes, and the exact next commands.

For a new hub:

```console
make deploy ARGS="--new-hub horde --target horde@20.115.163.162:2201"
```

Use `--ssh-port 2201` instead of an inline `:2201` when the target is an SSH
alias or otherwise contains a colon:

```console
make deploy ARGS="--new-hub horde --target horde@20.115.163.162 --ssh-port 2201"
```

Re-run deployment for an existing hub:

```console
make deploy HUB=horde
```

The deploy opens SSH with `BatchMode=yes`, so it can never prompt to accept an
unknown host key. On a brand-new box (no `~/.ssh/known_hosts` entry for the
target yet) the route probe therefore fails; it reports the classified cause —
`host-key-untrusted` — with the tail of the OpenSSH transcript and the fix.
Trust the key deliberately before deploying:

```console
ssh-keyscan -H <host> >> ~/.ssh/known_hosts
```

Alternatively, give the fleet an explicit `ssh_known_hosts_file` /
`ssh_host_key_fingerprint`, or set `ssh_host_key_policy: accept-new` in
`~/.mac/fleets.yaml` for a host you control. The other classified causes are
`host-key-changed` (the target's key no longer matches the pinned entry — never
paper over this one), `auth-rejected`, `unreachable`, and `timeout`.

The Make targets pick a Python 3.11+ `.venv/bin/python`, `python3.11`, `python3`, or `python` automatically:

```console
make setup
make deploy HUB=horde
```

MAC state lives under `~/.mac`. Hermes state lives under `~/.hermes`. The
in-mac LLM router, Qdrant, Firecrawl, the MAC API, worker services, and Hermes
bridge files are bootstrapped as part of the fleet service picture. Standalone
TokenHub is retired from the default fleet topology.

Private GitHub HTTPS repositories require a credential on every host or task
runner that performs Git work. An explicit deploy token may be kept in the
host-local `~/.mac/.env` (mode `0600`), never in `~/.mac/fleets.yaml` or a
committed fleet spec:

```console
# ~/.mac/.env -- placeholder only; supply the real value out of band.
MAC_DEPLOY_GH_TOKEN=<github-token-authorized-for-the-organization>
```

Fleet deploy resolves `MAC_DEPLOY_GH_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, then an
existing `gh` keychain login. It reports only the source name, streams the value
over SSH stdin, and writes it to the owner-only managed runtime as `GH_TOKEN`.
Pure `gateway_impl: none` workers verify the credential before drain or source
replacement and forward it to OpenShell through a private mode-`0600` file.
Kubernetes task and review Jobs instead read optional `GH_TOKEN`,
`GITHUB_TOKEN`, and `GITEA_TOKEN` keys from the runner's configured Kubernetes
Secret. Review Jobs do not receive `MAC_SECRET_KEY`.

## Where To Go Next

- [Hermes Integration](hermes-integration.md): how Hermes learns and uses the
  MAC vocabulary.
- [Hermes Boundary](hermes-boundary.md): what belongs to Hermes versus MAC.
- [Production Deployment](production-deployment.md): full deployment and
  operations detail.
- [Repository Runtime Contract](repository-runtime-contract.md): how registered
  project checkouts declare bootstrap and test commands.
- [Fleet Operational Learning](fleet-operational-learning.md): how repository
  access outcomes influence reviewer routing without storing credentials.
- [Soul Preservation Runbook](soul-preservation-runbook.md): how to restart
  agents without losing their Hermes identity and memory.
