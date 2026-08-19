# Getting Started

From nothing to a fleet that claims and publishes work. Every command here is
real; where a step can fail in a way that is hard to diagnose, this page says
so rather than assuming it will not happen.

## What you need first

- **Python 3.11+**. `make install` checks the interpreter *and* the one inside
  an existing `.venv`, recreating a stale environment rather than installing
  into it.
- **PostgreSQL.** The only supported backend. `scripts/start-test-postgres.sh`
  finds a local server or starts a container and prints the DSN.
- **`MAC_SECRET_KEY`**, 32+ characters. It derives the Fernet key for the
  secrets table. Without it the CLI and API both refuse to start — deliberately,
  because a control plane that boots without its secret key would be storing
  credentials it cannot protect.
- **SSH key access** to every host you plan to use, working *before* you begin.
  The setup wizard configures mac; it does not fix SSH.
- **At least one LLM provider key** (nvidia / openai / anthropic / perplexity).
  The wizard will not finish without one, because a fleet with no provider
  cannot execute a task.

## Install the CLI

```console
make install
```

This links `mac` into `~/.local/bin`, builds the Fleet IDE, and installs
CodeGraph if it is missing. CodeGraph is not needed to build or test mac, but
`litai init`, the skills and the coding-CLI paths expect it, so `install`
provisions it rather than leaving you to discover the gap later. Decline with
`MAC_SKIP_CODEGRAPH_INSTALL=1`.

```console
mac --version
```

## Create a fleet

Run the wizard on the machine that will be the hub:

```console
bash setup.sh
```

It asks two questions before anything else — whether you are on the machine
being configured, and whether this is a **hub** or a **worker**. Choose `hub`.

It then collects the fleet name, supervisor (`auto` selects launchd on macOS,
systemd on Linux), network provider (Tailscale by default), and your provider
key; writes `~/.mac/fleets.yaml` and `~/.mac/.env`; and deploys.

To write config without deploying:

```console
bash setup.sh --configure-only
```

Neither file belongs in version control. Fleet topology and provider keys are
yours, not the product's.

## Add workers

Run the wizard again on (or pointed at) each additional host and choose
`worker`. It looks up the fleet by hub name and asks only what is new: the
worker's name, SSH target, OS, supervisor, and mode.

Workers do **not** need a checkout of this repository — deploy ships the source
to each host.

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

```console
mac --fleet <name> task create "Add a --version flag to the CLI" \
    --description-file=brief.txt
```

Watch it move:

```console
mac --fleet <name> task list          # active work only, by default
mac --fleet <name> task show <id>     # state, history, evidence, reviews
```

Open `http://<hub>:8789/ui` in a browser for the live view.

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
mac admin fleet doctor
```

`mac task list` prints short ids (git-style, 8 hex) and accepts them anywhere a
full id is accepted.

## Next

- [Advanced Concepts](03-advanced.md)
- [The UI](04-ui.md)
