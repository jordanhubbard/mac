# Contributing to mac

The bar here is not "tests pass". It is **a reviewer can tell what would have
to be true for this to be wrong, and the tests check exactly that.**

Everything below is derived from failures this repository actually had. None of
it is style preference.

## Tests

Prefer observable behavior through public APIs over individual methods or
private helpers.

- **API contract tests** cover request/response schemas, status codes,
  documented errors, authentication, and backward compatibility.
- **API-level component tests** exercise the service through its public
  interface, with real internals and realistic persistence. Mock only true
  external boundaries.
- **Workflows** are complete scenarios (create → retrieve → update → delete,
  including failure recovery).
- **Edges** that matter: malformed input, missing data, duplicates,
  idempotency, authorization failures, timeouts, retries, partial failure,
  concurrency.
- **Method-level unit tests** only when the logic is complex, safety-critical,
  algorithmically subtle, or substantially easier to diagnose in isolation.

Do not add a test merely to raise count or line coverage. Do not duplicate the
same behavior at several layers, assert internal call sequences, test trivial
getters/wrappers, or lean on mocks. Before adding a test, name the distinct
contract, failure mode, boundary, or regression it protects.

Whole-repo coverage floors in `test-policy.toml` are collapse rails, not a
target to maximize. Do not add tests to satisfy them.

## Filing an issue

Issues live in the mac task ledger, not GitHub Issues:

```console
mac task create "one-line summary" --description-file=desc.txt
```

Use `--description-file` for anything with parentheses, backticks, `$VAR` or
newlines — shell quoting will otherwise mangle it silently.

A good issue answers four questions. The order matters, because most bad issues
skip straight to the fourth.

**1. What did you observe?** Real output, not a paraphrase.

```
mac task list --project nanolang
(none)

mac task list --project nanolang --all-states
task_de63afed  running  ...
```

**2. How do you know?** Numbers with their provenance. *"498 failures"* is a
claim; *"498 rows from `select count(*) ... where action_name=... and
timestamp > '2026-08-19T00:00'`"* is a measurement someone can re-run — and can
catch you being wrong. Two "measurements" filed here were later found to be
all-time totals from a broken timestamp filter.

**3. What is the actual mechanism?** Not the symptom. *"`mac task list` is
empty"* is the symptom; *"the CLI sends repeated `state=` and the route
declares `Optional[str]`, so FastAPI keeps only the last"* is the bug.

**4. What should happen instead?** Including what must NOT change.

If you cannot answer 3, file it anyway — say so explicitly. An honest *"I do
not know why"* is more useful than a confident wrong diagnosis, which sends the
next person somewhere real problems are not.

## A feature needs an ADR before it needs code

Change management here runs through architecture decision records in
`docs/adr/`, numbered sequentially. Anything that changes a contract, a schema,
a boundary between components, or how the fleet behaves gets one FIRST. Bug
fixes and mechanical changes do not.

An ADR is not a design doc and not a proposal to be approved. It is the record
of a decision and, more importantly, **of the alternatives that were rejected
and why** — so that the next person to have the same idea finds out it was
already considered, instead of relitigating it or quietly re-introducing it.

Follow the shape of the existing ones (`0016-agent-initiated-review.md` is a
good model):

- **Status / Date / Decision owner / Related** — link the ADRs this touches.
- **Context** — the measured problem. Numbers with provenance, same standard as
  an issue. An ADR whose context is "it would be nice if" has no way to be
  wrong, and no way to be checked later.
- **Decision** — what will be true, stated in the present tense.
- **Consequences** — including the bad ones. If reported cost jumps 30% the day
  this lands, say so here, or someone will read the step change as a runaway.
- **Alternatives considered** — each with the reason it lost.

Reference the ADR from the task and from the PR description. A PR that
implements a decision nobody recorded is how this codebase accumulated
subsystems whose rationale exists only in a chat log.

Prior art from other projects is worth reading and worth citing — ADR 0017
adopts a token-accounting model from `NVIDIA-dev/horde-claw-fleet` rather than
deriving one, and says so.

## Opening a pull request

### Work in a worktree

```console
git -C ~/Src/mac worktree add /tmp/mac-<task> -b <you>/<task>
```

Multiple agents run against this repository at once. Sharing the main checkout
has silently swept unrelated half-finished work into commits. Never `git add
-A` or `git commit -a` there.

### Check whether your task already has a PR

```console
gh pr list --search "<task_id>" --state all
```

A retried task currently opens a NEW pull request instead of updating the
existing one. On 2026-08-19 the open queue held 23 PRs covering 12 distinct
pieces of work — 11 were duplicates, one task having produced five of them from
two different agents, with five genuinely divergent implementations.

So: an open PR carrying your task id does not mean the work is finished, and it
does not mean you should open a second one. Read it, then either continue it or
say in your own PR why it was not salvageable.

If you are the second agent to arrive at a task that another agent is actively
running, stop. Concurrent work on one task is a claim bug, not a race to win.

### Before you push

```console
scripts/run-contract-tests.sh
bash scripts/dead-code-check.sh
uv run python scripts/generate-env-config-registry.py --check
uv run python scripts/generate-docs-reference.py --check
npx playwright test          # if you touched any UI
```

Regenerate what you invalidated. Adding an env var, a CLI flag or a route
staleness-fails a generated artifact, and the gate will catch it 40 minutes
later instead of now.

### Prove the tests would have caught it

The single most valuable thing in a PR description:

```console
git stash            # or revert just the source change
pytest -q tests/test_the_thing.py     # MUST be red
git stash pop
```

Then say so: *"11 of 27 tests fail without the change."* A test suite that
passes before and after the fix has verified nothing, and this is the cheapest
possible check that yours does not.

### Write the description for the next person

Explain **what was wrong**, **why it was not caught**, and **what you chose not
to do**. That last one prevents the same debate being re-run in six months.

The good ones here read like:

> `dependencies_width` was the only uncapped column. One task with six blockers
> took 49 columns and squeezed every title to 23 characters. The overflow marker
> carries a **count** — `[task_a,+4]` — because a bare truncation tells the
> reader neither how many blockers there are nor that any are hidden.

### Say what is still broken

If you fixed three of four cases, say which one you did not, and why. A PR that
implies completeness it does not have is worse than one that scopes itself
honestly. Several PRs here carry an explicit "what this does not do" section
and are better for it.

## What gets a PR sent back

- **A test that passes without the change.** It documents nothing.
- **A test whose only job is line coverage, a getter, a mock call sequence, or
  a duplicate of another layer.**
- **Weakening a behavioral gate to go green.** If `dead-code-check` fails, fix
  the code. Whole-repo coverage floors are collapse rails; do not add tests to
  hold them, and do not treat a floor miss as a reason to pad.
- **A generated artifact edited by hand.** Regenerate it. Hand-merging a
  generated file makes it a lie about what its generator produces.
- **A claim you did not verify.** "Should be fine on Linux" is not a result. If
  you did not run it, say you did not.
- **Silent scope reduction.** Capping, sampling or skipping is fine; doing it
  without saying so reads as "covered everything" when it did not.

## Reviewing

Ask three questions:

1. **What would have to be true for this to be wrong?** Is that checked?
2. **What did this stop testing?** A deleted or re-pointed assertion may have
   been the only thing pinning a real property.
3. **Does the failure mode announce itself?** The recurring bug class here is
   the *silent* one — a filter that matches everything, a write that updates
   zero rows and reports success, a gate that validates a proxy for the thing
   instead of the thing.

## The house rule

Prefer a system that fails loudly to one that looks healthy.

Nearly every expensive bug in this repository has been the same shape: a check
that validated something adjacent to what it claimed. A staleness gate that
checked function names instead of collected tests. A contract that checked
paths but not parameter shapes. A metadata write that matched zero rows and
returned success. An injection path fully wired to a value that was always
`None`.

Each looked correct and enforced nothing. When you add a guarantee, ask what it
would do if the thing it guards were already broken — and make sure the answer
is "say so".
