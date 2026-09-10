# A request people can verify

Start with the [supported setup](02-getting-started.md): PostgreSQL, the
configured gateway, one registered repository project, and one executable
worker. Keep the current generator yield gate and dispatch holds in place
while validating this path. Additional tasks or workers do not repair an
unreliable request-to-result loop.

## Define one observable result

Write a brief with the starting behavior, the requested behavior, one example
that demonstrates the change, and the repository's test command. Include the
scope boundary: what files or behavior the worker may change. Register the
repository from its checkout and use its returned project name explicitly.

```console
mac project register
mac project list
mac task create "Implement the behavior in brief.txt" --project <project> --description-file brief.txt
mac task show <id>
mac task why-unclaimed <id>
```

Use one small repository change as the canary. Require a real worker execution,
a review of the intended behavior, canonical publication, and a demonstration
of the resulting behavior. A fixture executor is useful for integration tests
but cannot prove that a live coding agent or deployed application works.

## Read the result before accepting it

```console
mac task outcome <id>
```

| Result | What it establishes |
|---|---|
| Tests: `reported_pass` | Current evidence contains passing test return codes. This is a recorded report, not a new verification run. |
| Acceptance: `accepted` | An authenticated operator recorded that the current evidence satisfies the request. It is bound to that evidence and attempt. |
| Publication: `published` | The completed task has a matching published record for its current executor evidence. Inspect the publication target and canonical proof in task detail. |
| Deployment: `recorded` | Deployment evidence explicitly links to this executor evidence. Read its contents; existence alone is not a live health check. |
| `unknown` | The matching evidence is absent. No success or zero is inferred. |

Inspect the diff, run the requested scenario, and record what you observed in
`acceptance.txt`. Copy the current executor evidence ID from the outcome:

```console
mac task accept <id> --evidence <evidence-id> --reason-file acceptance.txt
```

Use `--reject` when the result does not satisfy the request. Acceptance is an
operator write, unavailable to agent-bound tokens. It does not approve a review,
complete a task, merge code, or deploy it. A new attempt or new executor evidence
invalidates the displayed acceptance and requires a new decision. Avoid editing
a reviewed request to mean something different; file the changed request as new
work so its history and acceptance remain meaningful.

The shipped console's Task view shows the same evidence and task-specific CLI
handoffs. Use an authenticated profile for the same hub. Its HTTP layer remains
read-only; commands execute only when you run them.

## Recover with the existing lifecycle

Inspect `mac task show <id>` before every recovery. Preserve evidence, attempt
history, the existing branch/PR, and the current canonical revision.

| Failure | Smallest safe next step | Evidence of recovery |
|---|---|---|
| Worker disappears | Inspect agent health and the task lease. Let the hub reconcile expiry; diagnose with `mac task why-unclaimed` before reopening. Do not start a competing executor. | Old lease loses authority; only the new attempt may submit current evidence. |
| Repository tests fail | Read the full failed gate output. Correct the existing work in an isolated checkout and rerun the repository gate. | Passing results identify the revised evidence; no push from a failed gate. |
| Task asks a question | Use `mac task edit <id>` to read and answer the pending question. Check the answer disposition before resuming. | Answer and resulting state are durable in task history. |
| Canonical branch conflicts | Inspect the publication failure and existing PR. Rebase or resolve in the isolated task branch, rerun tests, and submit updated evidence through the review path. | Fresh evidence is verified against the current base; stale approval cannot land the old result. |
| Work must stop | Use `mac task stop <id> --reason-file stop.txt`. If abort fails, inspect the reported error before claiming the process stopped. | Hub confirms the stop and records the transition. |

These are recovery procedures, not claims that all live fault scenarios have
already passed. The repository's HTTP/process, lease, input-state, and native
merge-queue tests exercise controlled failures. Deployment verification must
record the actual revision, worker, task, evidence, publication, and observed
behavior separately.

## Measure a creation cohort

```console
mac task outcomes --project <project> --since-hours 168 --limit 100
```

This includes unfinished tasks in the denominator and groups results by recorded
origin type. It reports accepted completions, completion time, observed priced
model routes, and recorded operator interventions. Missing cost is `null`, not
zero. Route cost excludes uninstrumented model calls, infrastructure, and human
time. Intervention counts cover recorded acceptance, answers, and confirmed
stops; they cannot count work nobody recorded.

A truncated result is labelled a bounded sample, not the complete cohort. Widen
the limit (maximum 500) or narrow the creation window before comparing results.
Do not compare completion fractions from cohorts with different observation
ages as if they measured the same opportunity to finish. Keep autonomous task
generation bounded until comparable cohorts show accepted useful work without
increasing cost or operator repair effort.
