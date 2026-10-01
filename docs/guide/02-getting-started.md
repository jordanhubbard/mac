# Getting Started

From nothing to a fleet that claims and publishes work. Every command here is
real; where a step can fail in a way that is hard to diagnose, this page says
so rather than assuming it will not happen.

## What you need first

- **Python 3.14.7 and `uv`** (the reviewed version is in `.python-version`). `make install` checks the interpreter *and* the one inside
  an existing `.venv`, recreating a stale environment rather than installing
  into it.
- **PostgreSQL.** The only supported backend. `scripts/start-test-postgres.sh`
  finds a local server or starts a container and prints the DSN.
- **`MAC_SECRET_KEY`**, 32+ characters. It derives the Fernet key for the
  secrets table. Without it the CLI and API both refuse to start — deliberately,
  because a control plane that boots without its secret key would be storing
  credentials it cannot protect.
- **SSH key access** from the hub to every host you plan to use, working
  *before* you begin.
- **At least one LLM provider key** (nvidia / openai / anthropic / perplexity).
  A fleet with no provider cannot execute a task.

## Install the CLI

```console
make install
```

This links `mac` into `~/.local/bin` and builds the observability console.

```console
mac --version
```

## Create a fleet

Provision the hub, then each worker, by hand with the "Provision a new host"
checklist in [Updating the fleet with fleet-update](../operations/fleet-update.md).
Every host runs MAC from a git checkout at `~/.mac/src/mac`. Move hosts to a new
commit with `scripts/fleet-update`, run on the hub.

`~/.mac/fleets.yaml`, `~/.mac/fleet-hosts` and the env files don't belong in
version control. Fleet topology and provider keys are yours, not the product's.

## Check it came up

```console
mac --fleet <name> agent list
mac --fleet <name> task ready
```

`agent list` shows each agent's status and probed hardware. `task ready` shows
what could be claimed *right now* — open, unclaimed, no unfinished
dependencies.

If `agent list` shows an agent as `idle` that you know is down, trust the
process over the record: the hub stores a *reported* status, and the console's
Agents view exists to put that next to the last time the agent was actually
heard from.

## Run your first task

Use one repository and one executable worker first. From the repository you
want the fleet to change, register it and inspect the result:

```console
mac --fleet <name> project register
mac --fleet <name> project list
mac --fleet <name> task throughput
```

Use the returned project name explicitly. Write `brief.txt` with one small
change, the exact behavior to demonstrate, and the test command. Then create
one task:

```console
mac --fleet <name> task create "Implement the behavior in brief.txt" \
    --project <project> --description-file=brief.txt
mac --fleet <name> task show <id>
mac --fleet <name> task why-unclaimed <id>
```

Open `http://<hub>:8789/ui` for the live view. A pull request and passing tests
are intermediate evidence. Follow [the trust workflow](06-trust-workflow.md)
to check publication and explicitly accept the requested behavior.

## When a task does not move

This is the most common early frustration, and it has three usual causes.

**It was never claimable.** Ask before filing:

```console
mac task preflight --capabilities python --hardware '{"os":["linux"]}'
```

The classic mistake is asking for a *capability* that is really a host fact.
Agents advertise `python`, `testing`, `review`. They never advertise `linux` —
that is probed into `resources.hardware`. A task requiring capability `linux`
is accepted and then never claimed, with nothing obviously wrong.

```
WRONG   --capabilities linux
RIGHT   --hardware '{"os": ["linux"], "cpu_arch": ["x86_64"]}'
```

**It is blocked on something that can never finish.**

```console
mac task why-unclaimed <id>
```

A blocked task waiting on a `failed` or `cancelled` dependency will never
release under the default `all_success` join policy — only a *completed*
dependency releases it.

**It was filed with a dispatch hold.** `mac task create --no-dispatch` stages a
task without making it claimable. Release it:

```console
mac task release <id>
```

## Everyday commands

```console
mac task list                     # active work (add --all-states for everything)
mac task list --all               # every project, not just the inferred one
mac task ready --limit 10
mac task show <id>
mac task create "title" --description-file=f.txt
mac task ask <id> --question "..."     # park a task pending an answer
mac task answer <id> --answer "..." --disposition resume
mac agent list
mac agent hold/resume <id>
mac project list
mac admin diagnostics
```

`mac task list` prints short ids (git-style, 8 hex) and accepts them anywhere a
full id is accepted.

## Next

- [Advanced Concepts](03-advanced.md)
- [The UI](04-ui.md)
