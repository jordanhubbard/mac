# Can MAC do work? — fleet assessment, 2026-08-02

An evidence-based answer to a direct question: does this framework actually
complete work, is it moderately efficient at it, and is the design sound enough
to keep?

All figures are drawn from the live hub ledger (7,678 tasks), the `mac` git
history since 2026-07-01, and a controlled twelve-task experiment run on
2026-08-02.

## Verdict

**Keep it, and the fix is narrow.** The execution core works and is efficient.
The losses concentrate in one design decision — automatic decomposition of
tasks into dependency trees — plus a set of generators that manufacture work
nobody wants. Both are removable without touching the part that works.

## 1. It produces real work

Commits to `mac` since 2026-07-01, by author:

| Author | Commits |
|---|---|
| MAC fleet | 484 |
| operator (primary identity) | 327 |
| MAC certifier canary | 237 |
| operator (secondary identity) | 108 |
| other fleet identities | 16 |

Roughly **737 fleet-authored commits against 435 human ones**. 527 of the 1,196
commits in the window reference a task id. This is not a system that does
nothing.

## 2. It is efficient on independent work

| Metric | Value |
|---|---|
| Attempts per completed task | 1.21 |
| Median created → completed | 1.9 h (p75 8.4 h, p90 30 h) |
| Peak throughput | 80–229 completions/day, mid-July |

Given a self-contained task, the fleet does it once, quickly, and lands it.

## 3. The decisive number: dependencies

Completion yield by dependency count, across all 7,678 tasks:

| Dependencies | Tasks | Completed |
|---|---|---|
| 0 | 2,945 | **29.3%** |
| 1 | 2,888 | **5.5%** |
| 2 | 1,012 | **2.0%** |
| 3+ | 833 | **2.0%** |

**One dependency drops completion 5.3x. Two is effectively terminal.**

The system attaches them by default:

- 62% of all tasks carry dependencies
- 50% are children of a decomposition
- 82% of the `waiting` pool are decomposition children

### The controlled experiment

Twelve tasks were filed on 2026-08-02, each verified against current `main`,
self-contained, with **zero dependencies** and capabilities the fleet
advertises. The fleet claimed them within seconds and put seven agents to work.

**Zero completed.**

One is representative: a task to fix a `CARGO_HOME` probe was decomposed by the
planner into three children — implement, test, verify — with the parent made
dependent on all three. One child failed. The parent is now stranded
permanently.

That is the failure in one observation: the system converts a task it could do
into a tree it cannot.

## 4. Where the remaining loss goes

### Generators that manufacture unwanted work

Completion yield by task origin:

| Origin | Tasks | Yield |
|---|---|---|
| `direct_task` | 4,419 | 20.4% |
| `environment_prerequisite` | 594 | 9.6% |
| `contract_prerequisite` | 599 | 7.7% |
| `task_system_reset` | 137 | 2.9% |
| `crash_observer` | 50 | 2.0% |
| `dream_low_confidence_repair` | 1,396 | **0.3%** |
| `self_heal` | 46 | **0%** |

**3,259 tasks — 42% of everything ever filed — came from generators whose
combined yield is near 3%.**

The dream scanner's replacement records the verdict in its own source: *"in
forty days of production that produced 154,273 artifacts… and filed 1,259
investigation tasks of which 4 completed."* It has been deleted. The others
have not.

### Failures that were never failures

Of 3,245 tasks in `failed` before triage:

| Cohort | Count | What happened |
|---|---|---|
| Cascade | 1,348 | Never ran; auto-failed when a dependency died, under a rule since removed |
| Own failure, budget left | 1,508 | Stopped early |
| Genuinely exhausted | 389 | Tried until the retry budget ran out |

**Only 12% actually exhausted their attempts.** Triage of all 1,348 cascade
tasks against current `main` found 85% obsolete (843 moot, 308 already done),
13% still wanted, 2% needing a human decision.

Overall, **69% of all execution attempts ended in a failed task** (2,862
against 1,281).

## 5. What is genuinely working

This constrains the fix, so it is worth stating plainly.

- **The verification contract.** In the experiment, one task ran and the agent
  reported "56 passed total". The contract rejected it: `pushed=false`, no
  changed files. It correctly refused to bank work that was never landed. A
  share of the 547 `verification_contract_failed` results is this gate doing
  its job.
- **Fail-closed execution.** The executor refuses to launch without a sandbox.
  It cost tasks on two misconfigured workers, but refusing was correct.
- **The recursion guard.** 155 `repair_task_exhausted_no_recursion` results are
  repair-of-repair chains being correctly dead-lettered.

## 6. Recommended changes, in order of value

1. **Stop auto-decomposing tasks.** Dependencies cost 5.3x completion and buy
   nothing measurable. If decomposition is kept, a child's failure must reopen
   the parent rather than stranding it — 493 tasks are waiting today, 82% of
   them decomposition children.
2. **Retire or gate the remaining generators** — `self_heal` (0/46),
   `crash_observer` (1/50), `task_system_reset` (4/137). The dream-scanner
   deletion is the precedent.
3. **Verify the execution boundary at registration**, not at first task. A
   worker whose startup self-test passes while it cannot execute anything is
   the failure this assessment began with.

At the observed 29.3% independent-task yield, with generator noise removed,
roughly one filed task in three completes at 1.2 attempts. That is a system
worth running.

## 7. Limits of this analysis

- 361 of 1,061 completions record `attempt_count=0`; some are administrative
  force-completes, so raw completion counts are slightly generous.
- Commit authorship proves work landed, not that it was good work. Fleet commit
  quality was not audited.
- The dependency/yield correlation is strong but not proof of causation: harder
  tasks may attract more dependencies. The twelve-task experiment is the causal
  evidence, and n=12 is small.
- One repository, one fleet, roughly six weeks.

## Bottom line

The engine is sound. The scheduling and task-generation layers wrapped around
it are losing the work, and both were bolted on rather than being load-bearing.
The single highest-value change is to stop turning one task into four.
