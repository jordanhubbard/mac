# Crash diagnosis and autonomous repair

MAC fleet agents run beneath a supervisor-independent crash observer. The
observer is copied to `~/.mac/bin/mac-crash-observer` and deliberately uses the
host Python standard library instead of the MAC virtual environment. A broken
MAC installation therefore cannot disable its own crash reporter.

The native service manager remains responsible for restart:

- systemd uses `Restart=always` and launches the observer with
  `--supervisor systemd`;
- launchd uses `KeepAlive` and `--supervisor launchd`;
- supervisord uses `autorestart=true` and `--supervisor supervisord`;
- the Kubernetes orchestrator Deployment uses `--supervisor kubernetes` and a
  pod-local spool volume that survives container restart.

## Evidence lifecycle

Before starting the worker, the observer enables `PYTHONFAULTHANDLER`,
unbuffered stderr, and core dumps where the host permits them. It forwards
termination signals to the child, tees a bounded stderr tail, and distinguishes
intentional supervisor shutdown from unexpected exit.

For an unexpected exit it records:

- agent, process, supervisor, PID, exit code, and signal;
- source commit and tree;
- active task and lease when the hub remains reachable;
- fatal Python/native stack output and a bounded stderr tail;
- core path or systemd-coredump metadata/backtrace;
- disk, load, CPU, and child-resource observations.

Filesystem cores are hard-linked or copied into
`~/.mac/crashes/<event-id>/core` with mode `0600`. Retention defaults to the
three newest cores and refuses to copy a core larger than 512 MiB. Configure
those bounds with `MAC_CRASH_CORE_RETAIN_COUNT` and
`MAC_CRASH_CORE_MAX_BYTES`.

Every report is first written atomically to a mode-0600 JSON file beneath
`MAC_CRASH_SPOOL_DIR` (default `~/.mac/crash-spool`). Successful hub delivery
removes it; the next observer invocation replays any files retained during a
hub outage. `event_id` makes replay idempotent.

## Hub API

An agent-bound token submits its own evidence:

```text
POST /agents/{agent_id}/crash-reports
```

Operator projections are:

```text
GET  /crash-reports
GET  /crash-reports/{report_id}
POST /crash-reports/{report_id}/resolve
```

The list projection requires `read`. Full occurrence content requires `secret`
because stderr can contain private application output. Resolution requires
`admin`.

The hub redacts common credentials before persistence and computes the
fingerprint itself from the source revision, process, termination class, and a
normalized stack signature. Every occurrence remains durable while matching
events share one active incident.

## Repair policy

The first occurrence creates an immediately dispatchable P0 `mac` repair task.
Every affected agent is added to the task's hard `excluded_agent_ids`; the
dispatcher will wait rather than assign crash diagnosis to the revision/node
that crashed. If the chosen repairer later exhibits the same fingerprint, its
lease is released and another healthy peer takes over.

Hub ticks reconcile the repair task. Completion resolves the incident. Failure
or cancellation creates a new repair attempt that references the prior task.
After three failed autonomous attempts the incident becomes `needs_human` and
an operator notification is recorded.

## Verification

A deployment is conformant only when its installed service command contains
`mac-crash-observer`, the observer can post a synthetic non-zero child exit,
the hub retains the occurrence, and the resulting repair task excludes the
crashed agent. Restart policy alone is not crash-diagnosis conformance.
