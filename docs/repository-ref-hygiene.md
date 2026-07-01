# Managed Repository Ref Hygiene

MAC workers publish task-owned Git branches so execution and review evidence can
name an immutable commit. Those branches are temporary operational resources,
not a permanent backlog. MAC records their terminal disposition and provides a
fail-closed reconciler so they can be removed without guessing from branch age
or commit messages.

## Managed namespace

Automatic cleanup is limited to branches matching this shape:

```text
mac/agent_<agent>/task_<32-hex>-lease_<lease-fragment>
```

Manual feature branches, default branches, tags, legacy worker namespaces, and
lookalike refs are never eligible. The task ID and lease ID in the branch name
bind the ref to the authoritative MAC task record.

## Cancellation dispositions

Use a structured disposition whenever work is cancelled:

| Disposition | Meaning | Automatic cleanup |
| --- | --- | --- |
| `duplicate` | Another task owns the same work | After the replacement is named and the grace period expires |
| `superseded` | A replacement implementation makes this attempt obsolete | After the replacement is named and the grace period expires |
| `not_applicable` | The work is no longer required | After the grace period expires |
| `deferred` | The work still applies but should not run now | Never |
| `failed_attempt` | The attempt failed and may be needed for diagnosis | Quarantined; never without a later operator decision |
| `preserve` | The operator has not authorized deletion | Never |

Omitting a disposition defaults to `preserve`. `duplicate` and `superseded`
require `--replacement-task`, and every automatically cleanable cancellation
requires a reason. Audit also verifies that the replacement task has reached
`completed`; merely naming an open or unavailable task cannot authorize cleanup.

```bash
mac task close task_old --cancelled \
  --disposition superseded \
  --replacement-task task_replacement \
  --reason "replacement passed review and was published" \
  --cleanup-grace-days 7
```

The same command can classify an already-cancelled legacy task. MAC updates the
repository-ref lifecycle metadata without reopening the task or resetting its
original terminal timestamp.

Completed tasks receive an `integrated` lifecycle record. Failed tasks are
quarantined. Reopening a task immediately replaces any terminal cleanup schedule
with an active lifecycle record.

## Audit and prune

Refresh the canonical base ref, then audit from a checkout that has access to
the MAC hub and repository remote:

```bash
git fetch --prune origin
mac --json repo refs audit --repo . --remote origin --base-ref origin/main
```

Audit reads current branch SHAs with `git ls-remote`, loads each task from the
MAC ledger, checks open GitHub pull requests, and reports classification counts.
It does not update or delete remote refs. Classifications include `active`,
`blocked`, `deferred`, `superseded`, `quarantined`, `merged`, and `unknown`.

Prune is also a dry-run unless `--execute` is supplied:

```bash
mac --json repo refs prune --repo . --remote origin
mac --json repo refs prune --repo . --remote origin --execute --actor operator
```

Execution fails closed unless all of these checks pass:

- The branch is in the exact managed namespace.
- The task exists, is terminal, has no active lease, and has an eligible
  disposition.
- Its grace period has expired.
- A completed branch is proven reachable from the selected canonical base ref.
- GitHub pull-request state was successfully checked and no pull request is
  open for the branch.
- The remote SHA still equals the SHA observed by audit.

Deletion is one atomic Git push guarded by a per-ref `--force-with-lease` for
the audited SHA. A branch that moves between audit and deletion is retained.
Before the push, MAC records a `requested` evidence tombstone on every task;
after success it records `deleted`. Push failures record `failed`, with
authenticated URLs redacted.

Use repeated `--task` options to limit an audit or prune to selected task IDs.
The JSON report's `counts` and `eligible_count` fields are suitable for a daily
operator job and monitoring. A scheduler should run audit continuously and
enable `--execute` only after operators have reviewed legacy classifications.

## Lifecycle integration

Task transitions write `metadata.repository_ref_lifecycle` and emit a durable
`repository.ref.lifecycle` control-plane event. Network deletion deliberately
does not occur inside the task transaction: the reconciler performs Git and
GitHub checks later, where failures cannot corrupt task state.

This split gives MAC an automatic candidate lifecycle and an idempotent cleanup
boundary while preserving the independent evidence and review contracts.

## Recovery

If cleanup fails, run audit again. A remaining branch will be re-evaluated from
current task and remote state. If the branch was deleted but the final evidence
write failed, the pre-deletion `requested` tombstone still records the branch,
task, lease fragment, and exact SHA for reconciliation.

Do not bypass an `unknown`, `deferred`, or `quarantined` classification by
deleting the branch manually. Correct the task disposition or replacement link
first so the decision remains part of the durable audit trail.
