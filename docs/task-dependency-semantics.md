# Task dependency failure semantics

Task dependencies order work. They do not implicitly authorize cancellation of
the dependent task or its descendants.

The default policy is supervised:

- a completed prerequisite satisfies an ordinary dependency;
- a failed or cancelled prerequisite records a durable
  `mac.dependency_resolution.v1` outcome on each waiting dependent;
- ordinary and downstream dependents become `blocked` with
  `reason=dependencies_incomplete`, preserving their work and provenance;
- cooperative integration parents remain `waiting` and use an `all_settled`
  join, so they can inspect completed, failed, cancelled, and
  dependency-blocked child outcomes;
- independent children continue running;
- cooperative children opt into the existing bounded deterministic
  environment-repair prerequisite, whose recursion guard prevents
  repair-of-repair chains.

Cancellation is reserved for explicit policy or operator intent. A task may
request the old all-for-one behavior with:

```json
{
  "dependency_policy": {
    "on_unsatisfied": "cancel_scope"
  }
}
```

Cooperative decomposition writes the following policy onto the integration
parent:

```json
{
  "dependency_policy": {
    "on_unsatisfied": "supervise",
    "join": "all_settled"
  }
}
```

`all_settled` does not treat arbitrary blocked work as finished. A blocked
child counts as settled only when it carries a supervised
`dependency_resolution.status=unsatisfied` record. Executor failures that may
still retry or acquire an environment-repair prerequisite therefore continue
to hold the integration parent.

When a terminal prerequisite declares a live replacement, dependency
reconciliation rewires the edge to that replacement instead of recording an
unsatisfied outcome. Work-package task graphs retain their separate immutable
epoch transaction semantics and are not rewritten by the legacy task
reconciler.

## Required invariants

1. A child failure causes no derived cancellation under the default policy.
2. Independent siblings continue after another child fails.
3. A downstream child that cannot execute remains preserved and diagnosable.
4. The integration parent opens only after every child has a settled outcome.
5. Terminal-event ordering does not change the final integration readiness.
6. Only `cancel_scope` or an explicit operator cancellation may produce a
   cancellation cascade.
7. Replacement and repair creation remains bounded and idempotent.

The control plane emits `task.dependency_supervised` observations with
`derived_cancellations=0` so dependency-failure blast radius can be monitored.
