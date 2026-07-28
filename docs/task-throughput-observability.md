# Task throughput observability

MAC measures performance as completed work reaching the canonical project
branch, not request rate or CPU utilization. The operator surface is:

```console
mac task throughput
mac task throughput --project mac --since-hours 24
mac --json task throughput --all --warning-minutes 5 --critical-minutes 10
```

The command executes on the authoritative hub. It repairs at most 100 stale
task histories by default (`--refresh-limit`, maximum 500), then returns a
`mac.task_flow_snapshot.v1` document. This bound prevents an observability
query from becoming fleet-wide lock or overview-query load.

## Lifecycle and SLO

Every attempt is projected into these canonical stages:

1. intake/dependency wait
2. ready queue
3. claim to executor start
4. execution
5. review queue
6. active review
7. integration queue
8. integration and projected-main tests
9. publication
10. post-publication CI follow-up, when configured
11. finalization

The default basic-cycle objective is p50 at or below five minutes and p95 at
or below ten minutes. Ready-to-claim has a tighter p50 of 30 seconds and p95 of
two minutes. A task staying in one stage for five minutes opens a durable
`mac.task_stranding_episode.v1`; ten minutes makes it critical. Repository test
latency remains visible as its own stage rather than being hidden or treated as
a reason to relax every other queue.

Lifecycle writes update only the current stage. A report performs an
idempotent, bounded reconstruction from `task_history`, `reviews`, and
`publications` when a task ledger is newer than its materialized flow. This
keeps the write path constant-time while allowing older and crash-interrupted
records to converge.

## Throughput factors and evidence

| Throughput factor | Observable evidence |
| --- | --- |
| task intake, dependencies, project pause, `no_dispatch` hold | intake/ready dwell plus authoritative dispatch reason |
| dispatcher backlog and claim latency | ready-queue distribution, eligible-agent snapshot, stranded reason |
| worker availability and capacity | idle worker count, dispatch candidates, agent/machine health |
| capability, platform, role, hardware, or tenant mismatch | dispatch rejection reason codes |
| worker startup, sandbox/bootstrap, source checkout, coding CLI availability | claim-to-start and execution dwell; worker evidence and failure history |
| DIND/OpenShell/runtime limitations | execution history, runtime evidence, capability mismatch |
| credentials, repository authentication, and key rotation | fleet-learning records and task failures; credential values are never stored |
| model routing, token use, provider retry/fallback | completion counters plus task LLM usage/profile |
| evidence production and reviewer availability | execution, review-queue, and review distributions |
| concurrent ref/path edits and merge conflicts | `mac.task_resource_contention.v1` keyed by hashed repository ref/path set |
| integration/rebase/test serialization | integration queue/test dwell and contention events |
| repository test/lint/CodeGraph gate latency | integration/test stage and worker evidence |
| publication, branch protection, submit queue, push failures | publication dwell and publication history |
| repository CI | CI follow-up stage and CI monitor records |
| finalizer/reconciler/outbox delay | finalization dwell, transition outbox, reconciler diagnostics |
| database locks, connection-pool saturation, API overload | request/SQL observability and lifecycle stage gaps |
| provider quotas, compute/GPU scarcity, disk/cache/port/service collisions | resource contention events and provider/runtime telemetry |
| stale sandboxes, worktrees, refs, or worker instances | cleanup/reconciler diagnostics and executor failure history |

The table is a coverage inventory, not a claim that every provider already
emits the generic contention record. Merge conflicts over a canonical ref and
conflicted path set are instrumented now. Other shared-resource owners should
call the same recorder when they wait or reject work, using a bounded
`resource_class` and a hashed secret-free resource identity.

## Durable schemas

- `mac.task_flow_span.v1`: one materialized stage per task attempt, with
  cumulative duration when a stage is revisited.
- `mac.task_completion.v1`: attempt-level task-to-main duration, stage
  durations, publication identity, review/rebase/test and routing-cost signals.
- `mac.task_flow_snapshot.v1`: bounded KPI window, active WIP, SLO compliance,
  stranding, and contention summary.
- `mac.task_stranding_episode.v1`: opens once per task/attempt/stage and is
  updated until progress resolves it.
- `mac.task_resource_contention.v1`: collision reason, stage, peer task IDs,
  wait interval, outcome, and a SHA-256 resource identity; raw credential or
  repository secrets are not stored.

`mac task show <id>` includes the task's `flow.spans` and
`flow.completions`, so a single failed-task inspection shows where time was
spent without launching another model or overview query.
