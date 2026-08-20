# ADR 0022 - A gate returns a named decision, not a boolean

- Status: **Proposed**
- Date: 2026-08-20
- Decision owner: MAC fleet owner
- Related: ADR 0013 (one authoritative hub allocator), ADR 0016 (agents decide
  what a task needs), ADR 0019 (privilege is an ACL on a resource tree)
- Prior art: `NVIDIA-dev/horde-claw-fleet` — `fleet-domain/src/scheduler.rs`,
  ADR-0121 (retry and terminal reconciliation semantics), ADR-0125 (task
  runtime efficiency and worker throughput)

## Context

Four separate gates failed silently on 2026-08-19/20, and each cost real time:

- `mac task why-unclaimed` printed a title and two attempt counters for a task
  no agent could take. The payload had every reason; the renderer dropped it.
- `_advisory_health_dispatch_ready` returned `False` for a worker that was
  idle, heartbeating and fully capable — and said nothing. It sat out beside 84
  open tasks.
- The contract gate failed 24 of 24 tasks with one message, `verification
  contract failed — work was not pushed/accepted`, which is true of every
  failure and diagnostic of none.
- `mac project pause` returned success and did nothing.

The common shape is not "the gate was wrong". Three of the four made the right
decision. The defect is that **a decision was reduced to a boolean before
anyone could ask why**, and the reason was then either reconstructed elsewhere,
approximated in prose, or lost.

### What the parallel fleet does instead

`horde-claw-fleet` dispatches Rust build/test jobs — a different problem — and
solved this shape explicitly. Its scheduling decisions live in a pure domain
crate whose module doc states the boundary:

> Store and worker callers provide typed model evidence; this module owns the
> branch decisions while SQL loading and durable mutations stay outside
> fleet-domain.

Every decision is a function from a typed `*Evidence` value to a typed
`*Decision` enum, and **every rejection is a named variant**:

    pub fn decide_retry_success_supersession(
        evidence: RetrySuccessSupersessionEvidence<'_>,
    ) -> RetrySuccessSupersessionDecision {
        if !lineage_is_eligible(..)  { return IgnoreUnrelatedAttempt; }
        if !replacement_succeeded(..) { return IgnoreNonSuccessfulReplacement; }
        if !prior_terminal_evidence_present(..) {
            return IgnorePriorWithoutTerminalEvidence;
        }
        SupersedePriorFailure
    }

There is no way to answer "no" without saying which "no". The explanation is
not a second system that has to agree with the first; it *is* the return value.

Two consequences follow that MAC does not get today. The decision is pure, so
it is testable without a database, a fleet, or a live task. And because the
reasons are an enum rather than free text, they are exhaustive and checked —
adding a rejection path without naming it does not compile.

## Decision

### 1. A gate returns a decision value that names its reason

Any function that decides whether something may proceed returns a value
carrying the outcome AND the reason. Not `bool`. Not `Optional[X]` where
`None` means "refused, work out why yourself".

In Python that is a small frozen dataclass with an outcome, a reason code from
a closed set, and whatever evidence made the decision. `mac.acl.Grant` is
already this shape and is the model to follow; the difference is that its
reason is a string, and reason codes should be a closed enumeration so a new
rejection path cannot be added anonymously.

### 2. The decision is separated from fetching what it decides on

The gate takes an evidence value; it does not go and load things. Fetching
belongs to the caller.

This is what makes a gate testable in isolation, and it is the reason MAC's
current gates are not. `_advisory_health_dispatch_ready` reaches into
`agent.resources` JSON; `_required_scope` was 204 lines of control flow that
had to be *read* to learn what it enforced. `mac.acl.AclEvaluator` is already
built this way — pure, no I/O — which is why 28 adversarial tests could be
written against it directly.

### 3. Reason codes are the vocabulary the operator sees

The code a gate returns is the code that appears in `why-unclaimed`, in task
history, and in observability. One vocabulary, not a rendering layer that
paraphrases an internal enum into prose and drifts.

### 4. This applies to the gates we already have

`_advisory_health_dispatch_ready`, the allocator's task/agent gates, the
contract gate, and the ACL evaluator. The contract gate matters most: 24 of 24
failures shared one message, so the ledger cannot distinguish "nothing was
pushed" from "the push was rejected" from "the executor never got far enough
to push". Those need different responses and currently look identical.

## Consequences

- "Why did this not happen?" becomes a property of the decision rather than an
  investigation. That is worth more than it sounds: three of the four silent
  gates above were *correct*, and the cost was entirely in finding out why.
- Gates become unit-testable without a fleet, which is how the ACL evaluator
  got adversarial tests and mutation checks before anything depended on it.
- Rejection paths cannot be added anonymously — a new outcome needs a new code.
- More types, and a mechanical migration across existing call sites. The
  boolean-returning gates are load-bearing, so they change one at a time with
  the old behaviour pinned by a test first.
- A closed reason vocabulary is a compatibility surface once it reaches
  operators and dashboards. Renaming a code later is a breaking change; the
  codes deserve the same care as an API.

## Alternatives considered

**Log the reason and keep returning a boolean.** Rejected: this is what the
contract gate does. The reason exists, in a log, disconnected from the decision
the ledger records — so the task says `verification_contract_failed` and the
explanation lives somewhere an operator has to go and correlate by timestamp.

**Raise an exception carrying the reason.** Reasonable for hard errors, wrong
here. Refusal is an ordinary outcome of a gate, not an exceptional one; making
the common path an exception means every caller wraps a try/except and the
"allowed" case reads as the anomaly.

**Reconstruct reasons in the renderer.** Rejected — this is precisely how
`why-unclaimed` broke. The payload had the reasons; a second system decided
what to show and silently showed nothing. Two systems that must agree about
why something was refused will eventually disagree.
