# ADR 0016 - Agents decide what a task needs; review is agent-initiated

- Status: **Proposed**
- Date: 2026-08-17
- Decision owner: MAC fleet owner
- Related: ADR 0011 (hub review verification uses affected tests), ADR 0013
  (one authoritative hub allocator), ADR 0014 (visibility is a communication
  boundary, not a dispatch gate)

## Context

MAC today runs a **mandatory, hub-mediated task workflow**. When a repository
task finishes, the hub drives it through `RUNNING → NEEDS_REVIEW → REVIEWING →
COMPLETED` (`src/mac/review_service.py`), and the default-review controller in
`src/mac/services.py` (`advance_default_review_workflow` and the sweep loop)
selects a reviewer, waits for a verdict, and only then allows publication. The
hub decides *what a task needs* — whether it must be reviewed, decomposed, or
routed to particular hardware — from `required_capabilities` and task metadata
guessed at filing time.

`required_capabilities` is a guess made before anyone has read the diff.
`--no-decompose` (`src/mac/cli.py`, `src/mac/task_lifecycle.py`,
`src/mac/services.py`) exists only because the hub cannot tell an atomic task
from a splittable one and needs an operator override. Whether a change actually
needs review, a GPU, or a second opinion is knowable only *after* reading the
change — information the executing agent holds and the hub does not.

### The evidence from one session (2026-08-16/17)

The *work* succeeded on the first attempt: the agent read the task, fixed the
target function, wrote a focused unit test, ran it against the test Postgres,
and ran a CodeGraph audit confirming caller control flow was unchanged.

Everything that failed was hub-mediated ceremony:

- 764,172 tokens burned and the task dead at attempt 1/3, because the executor
  entered a mandatory decomposition phase it was not authorised to complete,
  then emitted `plan_decomposed` evidence with zero children, which the
  contract gate correctly rejected as self-contradictory.
- 52 reviews across 38 tasks produced exactly four distinct reason strings and
  zero findings — a mandatory step contributing no measurable signal.
- One task was destroyed by three review rejections that were harness faults,
  not judgements, spending its whole retry budget on the mandated step.
- 18 ready tasks, 3 idle agents, 0 assignments, because the hub was
  arbitrating who may work on what.
- `workflow.default_review.waiting_for_hub_verify` polling: the hub relaying a
  conversation between two agents that could have spoken directly.

## Problem

The mandatory workflow puts the least-informed party (the hub, at filing time)
in charge of deciding what a task needs, and turns a synchronous
agent-to-agent conversation (a review) into a hub-polled state machine. That
costs tokens and retry budget without producing review signal, and it strands
ready work behind arbitration.

At the same time, the parts of the system that demonstrably worked are exactly
the *deterministic gates* — the contract gate, dead-code contract, impact-map
staleness check, `test_no_dead_indexes`, the CLI coverage gate, the reviewed-CLI
classifier, and the `tested-<inputs-sha>` image tag. Each named the defect and
the fix cheaply and reproducibly. Those are not opinions the hub must broker;
they are checks an agent can run.

## Decision

Move the *decision authority* to the agent and reduce the hub to a resource
orchestrator and capability registry.

1. **Agent-side skills decide what a task needs.** Reading the diff, the agent
   answers: is this atomic? does it need review? do I have the hardware to test
   it? who should I ask? am I saturated? These are skills the agent runs, not
   states the hub imposes. The deterministic gates in "Problem" become skills
   the agent runs locally and records, per the literate-AI thesis: where
   structure makes a breaking change obvious, review is verification rather
   than opinion.

2. **Review is peer-to-peer over AgentBus.** When a skill decides a change
   should be reviewed, the agent asks a peer directly over AgentBus, receives
   findings, and the review plus its findings are recorded in the ledger
   exactly as today. Choreography is peer-to-peer; the record stays
   centralised.

3. **The hub keeps only durable, uncontested authority:** the task ledger
   (find work, claim, close), the capability registry (what each agent can
   do), and capacity provisioning (ask for more workers when saturated). It
   stops being the arbiter of what a given task needs in order to finish. This
   extends ADR 0014's principle — visibility and coordination are communication
   boundaries, not dispatch gates — from *who may talk* to *what a task needs*.

### What must not be lost

1. **Evidence in the ledger, even when the conversation is peer-to-peer.**
   Otherwise reviewer value becomes permanently unmeasurable — the exact
   problem solved by persisting review findings. The peer-to-peer review path
   must still write the review row and its findings through the existing
   ledger APIs (`request_review` / review + findings records), so
   reviewer signal remains queryable regardless of who initiated the review.

2. **Liveness.** Lease expiry is how a dead agent's work is reclaimed. A
   synchronous peer wait must carry a bounded deadline and a defined fallback,
   or lease expiry gets reinvented — worse — in several places. A review
   request that is not answered before its deadline falls back to completing
   without peer review (recording that the request timed out) or to the
   existing hub path, never to an unbounded wait.

3. **The gates.** The deterministic contract/dead-code/impact/coverage/image
   gates stay. They move from hub choreography to skills the agent runs, but
   their pass/fail signal and evidence are unchanged.

### First step — deliberately small and reversible

For one project, make review **optional and agent-initiated** behind a single
flag, changing nothing else:

- The agent decides via skill whether to request review.
- If it does, it asks a peer over AgentBus, receives findings, and records the
  review and findings in the ledger exactly as today.
- If it does not, the task completes without a mandatory review step, and that
  decision is observable in the ledger.

The mechanism reuses machinery that already exists rather than adding a parallel
one. Per-task review policy already supports disabling the mandatory workflow:
`_default_review_policy` reads a `review` / `default_review` metadata object and
`_default_review_disabled` honours `mode: manual`, `manual: true`,
`enabled: false`, or `auto: false`; `advance_default_review_workflow` then
no-ops with a `workflow.default_review.skipped` observation. The first step is
to make that per-task opt-out the *project default* for one project — an
`agent_initiated` (opt-in) review mode — so the mandatory controller stands
down for that project and review runs only when an agent asks for it. This
mirrors the opt-in shape of `MAC_REVIEW_HUB_VERIFY` (`_hub_review_verify_enabled`,
off by default, one flag): a single, revertible switch.

Then compare completion rate and tokens-per-completed-task between the two paths
on the same project over a few hundred tasks (the arms already tracked by the
scientific optimizer). If peer-to-peer wins, there is evidence to dismantle the
ringmaster incrementally. If it loses, one flag is reverted and the mandatory
path is unchanged.

### Prerequisite

Concurrency-safety (tracked separately). Agents serving reviews for each other
while multi-tasking makes today's latent thread bugs primary, so the
peer-review server path must be brought under the concurrency-safety work
before it is enabled beyond the first project.

## Consequences

- The hub's job shrinks to resource orchestration and a capability registry.
  What agents are *doing* becomes state inside the agents; the hub is consulted
  to find work, claim it, close it, or ask for more workers.
- Review stops being an unconditional tax. It runs when a skill says the change
  warrants it, and its findings are still persisted, so reviewer value stays
  measurable and the "zero findings across 52 reviews" pattern becomes visible
  as *review not requested* rather than *review requested and empty*.
- A synchronous peer review introduces a new wait that must be bounded. The
  deadline-and-fallback rule keeps lease expiry as the single liveness
  mechanism instead of spawning per-review timeouts.
- The change is reversible at project granularity by one flag, so the
  completion-rate / token comparison can run as a controlled experiment before
  any hub choreography is removed.
- The trade-off is that agent-decided review can skip a review a mandatory
  policy would have run. The deterministic gates (which stay, as skills) bound
  that risk for structural defects, and the ledger record of *review not
  requested* makes any resulting regression attributable rather than silent.
