# Task scope packet

A task is not dispatchable until its scope is bounded.

Filing an unbounded task is legal — the ledger is an inbox and a place to
think. What changed is that an unbounded task is no longer unbounded
*silently*: filing one says what is missing, `mac task preflight` reports it as
a finding of its own, and `mac task why-unclaimed` names it as the gate.

## Why

MAC used to file a title and a description and let the worker work out the
scope. That is the input to a failure that dominated 2026-08-19/20: a worker
reads an unbounded task, concludes it must decompose, cannot create children,
emits `plan_decomposed` with zero children, and dies non-retryable at attempt 1
of 3. Every one of the 24 failures in that window was the contract gate
rejecting work that was never bounded enough to do.

Two earlier fixes went at the executor — stop it entering a phase it cannot
complete, and move the sizing decision to the agent. Neither addresses the
input. An agent deciding well still needs something decidable.

The clearest case was a two-line rename described thoroughly. The sizing
heuristic reads the prose *about* the work, not the work; a careful description
scored "large", the task was split into "a rename child plus two test
children", and it died. It was already atomic. What was missing was any
statement that it was.

The prior art is `horde-claw-fleet`'s `fleet-plan-scope` skill and its
ADR-0121, which requires a scope packet before write authority is granted. Its
field vocabulary is deliberately **not** copied: it is shaped around a
task-graph model with ownership slices, first inspection points and node
dependencies, and MAC has no task graph.

## The fields

`metadata.scope_packet`, four required statements:

| Field | Answers |
| --- | --- |
| `outcome` | the ONE thing that must be true when this task is done |
| `current_state` | what is true at the current head that the outcome contradicts |
| `surface` | the repository paths this worker owns — its write authority and its search bound |
| `validation` | the check that proves the outcome |

Three optional ones are recorded when present and never required:
`exclusions` (narrow the surface), `start_at` (where to look first), and
`notes`.

Dependencies are not a packet field: in MAC they are ledger edges
(`dependencies[]`) and the allocator already gates on them. Search bounds and
the ownership slice are the same thing in a worktree, so they are one field.

```console
mac task create "Drop the unused task_id parameter from _assert_task_actor" \
  --metadata '{
    "scope_packet": {
      "outcome": "_assert_task_actor takes no task_id and all call sites are updated",
      "current_state": "it accepts a task_id it never reads, implying a per-task check it does not perform",
      "surface": ["src/mac/services.py"],
      "validation": "pytest -q tests/test_control_plane.py -k actor"
    }
  }'
```

`surface` accepts a bare string for a one-file change. Every required field
must be a statement rather than a placeholder — `tbd`, `n/a`, `?` and friends
count as absent, because a field filled in to get past the gate has not
answered it.

## Checking before you file

```console
mac task preflight --capabilities python \
  --scope-packet-file scope.json --strict --require-scope
```

Two findings, reported separately because they have opposite remedies:

- `missing_capabilities` / `hardware_reasons` — no agent in the fleet can
  satisfy this. Change the fleet.
- `scope` / `scope_bounded` — the task never resolved its own boundary. Change
  the task.

`--strict` keeps its existing meaning (exit non-zero when nothing could claim
the task). Add `--require-scope` to make an unbounded scope fatal too.

## Enforcement

The gate is **off by default** and enabled per project:

```console
mac project update <name> --metadata '{"require_scope_packet": true}'
```

Every task already in the ledger predates the packet, so a default-on gate
would strand an entire backlog in one commit. The decision is computed and
reported either way — `mac task why-unclaimed` prints the scope verdict for a
project that has not opted in, marked as advisory — so a project can see what
enforcing would cost before it enforces.

Where the gate *is* enforced, the allocator rejects the task with
`task_scope_unbounded`, which is a named reason rather than a boolean, per
[ADR 0022](../adr/0022-a-gate-returns-a-named-decision-not-a-boolean.md). The
break-glass path bypasses it, as it does project registration and pause: a
recovery does not stop for paperwork the outage did not file.

## Effect on sizing

A bounded packet is the submitter's answer to the question the sizing heuristic
is guessing at, so when it is present the heuristic stops guessing: the scope
estimate is `small` and the executor does not enter a planning phase, however
long the description is. An explicit `metadata.plan_first` still wins — the
same precedence `no_decompose` already has over a decomposition budget — and an
*incomplete* packet buys nothing, so `{"outcome": "fix it"}` is not a way to
switch the heuristic off.
