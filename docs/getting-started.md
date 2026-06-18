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
- Beads repository: a repository whose `bd` issues are bridged into MAC.
- Project item: imported external work, such as a Beads issue.
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
- Secret: a credential or token, routed through TokenHub in fleet deployments.
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

```bash
cd ~/Src/mac
python3 scripts/bootstrap-project.py
PATH=.venv/bin:$PATH .venv/bin/python -m pytest
```

Create a local secret key. MAC uses this to protect secret records. Keep the
value private.

```bash
export MAC_SECRET_KEY="$(openssl rand -base64 32)"
```

Create a local database:

```bash
uv run mac --db mac.db init
```

Create a practice project and task:

```bash
uv run mac --db mac.db project create demo \
  --description "A safe local project for learning MAC"

uv run mac --db mac.db task create "Write a hello-world note" \
  --project demo \
  --description "Create a tiny file and record what was done." \
  --required-capabilities docs

uv run mac --db mac.db task list
```

At this point MAC has durable state: a project and a task. No agent has done the
task yet. You have created the official work record.

## Run The API And Dashboard

Start the API:

```bash
MAC_SECRET_KEY="$MAC_SECRET_KEY" uv run uvicorn mac.api:app --reload --port 8789
```

Open the dashboard:

```text
http://127.0.0.1:8789/ui
```

In another terminal, inspect the same state through the hub CLI:

```bash
uv run hgmac --url http://127.0.0.1:8789 projects list
uv run hgmac --url http://127.0.0.1:8789 tasks list
```

If you configured `MAC_API_TOKEN`, pass it with `--token` or store it in the
`hgmac` config file. With no API token configured, the local development API is
open on localhost.

## Split A Large Task

If an agent claims a task and decides it is too large, it should add child tasks
instead of trying to finish everything in one step. The parent task becomes
blocked until the children complete.

```bash
uv run hgmac --url http://127.0.0.1:8789 tasks add-child task_... \
  --title "Write the first draft" \
  --description "Produce the first small deliverable."
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

```bash
scripts/setup-fleet.py --list-samples                  # browse per-CSP samples
scripts/setup-fleet.py --init-from gke --name my-gke   # -> ~/.mac/specs/my-gke.fleet.yaml
$EDITOR ~/.mac/specs/my-gke.fleet.yaml                 # fill in the <placeholders>

mac fleet validate --spec ~/.mac/specs/my-gke.fleet.yaml
mac fleet doctor --spec ~/.mac/specs/my-gke.fleet.yaml
make setup ARGS="--spec ~/.mac/specs/my-gke.fleet.yaml --force"
```

See `deploy/fleet/samples/README.md` for the per-CSP convention. Never check a
named fleet into the repo.

The doctor report is JSON and calls out missing provider env vars, bad targets,
sample-config mistakes, and the exact next commands.

For a new hub:

```bash
make deploy ARGS="--new-hub horde --target horde@20.115.163.162:2201"
```

Use `--ssh-port 2201` instead of an inline `:2201` when the target is an SSH
alias or otherwise contains a colon:

```bash
make deploy ARGS="--new-hub horde --target horde@20.115.163.162 --ssh-port 2201"
```

Re-run deployment for an existing hub:

```bash
make deploy HUB=horde
```

The Make targets pick a Python 3.11+ `.venv/bin/python`, `python3.11`, `python3`, or `python` automatically:

```bash
make setup
make deploy HUB=horde
```

MAC state lives under `~/.mac`. Hermes state lives under `~/.hermes`. TokenHub,
Qdrant, Firecrawl, the MAC API, worker services, and Hermes bridge files are
bootstrapped as part of the fleet service picture.

## Where To Go Next

- [Hermes Integration](hermes-integration.md): how Hermes learns and uses the
  MAC vocabulary.
- [Hermes Boundary](hermes-boundary.md): what belongs to Hermes versus MAC.
- [Production Deployment](production-deployment.md): full deployment and
  operations detail.
- [Repository Runtime Contract](repository-runtime-contract.md): how registered
  project checkouts declare bootstrap and test commands.
- [Soul Preservation Runbook](soul-preservation-runbook.md): how to restart
  agents without losing their Hermes identity and memory.
