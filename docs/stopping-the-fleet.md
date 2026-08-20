# Stopping the fleet

Stopping the fleet is one command, and one command tells you whether it worked.

```console
$ mac admin fleet stop --drain --reason "hub migration window"
$ mac admin fleet status
stopped: 0/10 agents dispatchable, 10 held (10 by fleet stop), 0 task(s) in flight
$ mac admin fleet start
```

`mac fleet ...` is the same thing: the top-level spelling redirects to
`mac admin fleet ...`.

## Why this exists

Stopping the fleet was always *possible*. An audit that actually did it found
that every step succeeded, nothing needed escalation, and no permission was
refused — the operator credentials were adequate. What was missing was that the
operations did not exist as operations. A stop was an improvised sequence:

1. snapshot every agent's status and `dispatch_hold` by hand, so it could be
   restored afterwards;
2. run `mac agent hold` once per agent;
3. discover that a hold does **not** drain — agents were still executing tasks
   after every agent was held;
4. chase the in-flight tasks individually to reach quiescence;
5. answer "is the fleet actually stopped?" by piping `mac agent list --json`
   through a script.

That is five things to get right under pressure, which is exactly when
improvisation fails.

## The three verbs

### `mac admin fleet stop`

Holds every agent so no new work is dispatched, and records the reason on each
hold.

| Flag | Effect |
| --- | --- |
| `--reason TEXT` | Recorded on every hold this command places. |
| `--drain` | Also wait for work already executing, reporting what it waits on. |
| `--timeout SECONDS` | How long a `--drain` waits before failing (default 900). |
| `--poll-interval SECONDS` | How often the drain re-checks (default 5). |

Agents that were **already held** keep the reason they were held for. The stop
reports them separately rather than overwriting the record of why a machine was
quarantined.

The result carries a `snapshot` of every agent's pre-stop dispatch state, so
"what did this look like before I touched it?" is answerable from the output of
the command that touched it.

### `mac admin fleet start`

Restores the pre-stop state. Only the holds placed by a fleet stop are
released; an agent quarantined for a bad disk, a bad build or an open
investigation stays held, and is listed under `kept_held`.

Restoring *every* agent to "not held" is a different operation and usually the
wrong one, so it has to be named: `--release-all`.

### `mac admin fleet status`

One line: `stopped`, `draining` or `running`, with the counts and what is still
executing.

* **running** — at least one agent can still be handed new work. A partial hold
  is not a stop.
* **draining** — nothing can be dispatched, but work already accepted is still
  executing.
* **stopped** — nothing can be dispatched and nothing is executing.

## Hold is not drain

`dispatch_hold` stops *new* dispatch to an agent. It says nothing about the
task that agent is already running. These are two separate operations and the
commands keep them separate:

```console
$ mac admin fleet stop                      # closes the door
$ mac admin fleet status
draining: 0/10 agents dispatchable, 10 held (10 by fleet stop), 2 task(s) in flight
  task_4d756013  running  agent_worker-1
  task_9c1a77f0  claimed  agent_worker-2
```

`--drain` additionally waits, naming what it is waiting on at every poll (on
stderr, so `--json` stdout stays parseable). A drain that times out exits
non-zero and lists the tasks still executing — a stop that reported success
while tasks ran would be the failure this whole surface exists to remove.

A drain waits for `claimed` and `running` work only. `needs_review` and
`needs_input` are parked awaiting a human, so a drain that waited for them
would never finish.

## How the restore knows what to restore

The fleet stop marks the holds it places with a `fleet-stop:` prefix in
`dispatch_hold_reason`, and that marker *is* the snapshot: a hold without it was
somebody else's decision, and `fleet start` leaves it alone.

Keeping the snapshot in a file beside the operator would strand it on one
workstation and go stale the moment anyone held an agent by hand. Keeping it in
a new hub table would need a schema, a route and a migration for one boolean per
agent. The marker is durable, shared by every operator, and self-repairing: a
hold placed by hand *during* a stop is simply not the stop's to release.

## Related fixes

Two commands on this path used to exit 0 without doing what they said, which is
worse than failing:

* `mac project pause` set `metadata.dispatch_paused` but left the project's
  visible `status` at `active`, so the field an operator reads back to confirm
  said nothing had happened. The status now moves to `paused`. Resuming a
  project that is `inactive` or `archived` is refused rather than silently
  reported as success — that status was not set by a pause, and clearing the
  flag would not make the project dispatchable. Use
  `mac project update <name> --status active` for that.
* `mac task update <id> --dependencies <other>` had no such flag, so it reached
  argparse as an unrecognized argument while the operator read the outcome as
  accepted. The flag now exists and replaces the dependency set; a task that
  gains dependencies moves to `waiting`, and an empty value clears them.
