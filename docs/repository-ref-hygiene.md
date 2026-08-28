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

```console
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

```console
git fetch --prune origin
mac --json admin repo refs audit --repo . --remote origin --base-ref origin/main
```

Audit reads current branch SHAs with `git ls-remote`, loads each task from the
MAC ledger, and checks open reviews on the selected remote. GitHub remotes use
an authenticated `gh`; GitLab remotes use an authenticated `glab`. It reports
classification counts without updating or deleting remote refs. Classifications
include `active`, `blocked`, `deferred`, `superseded`, `quarantined`, `merged`,
and `unknown`.

Prune is also a dry-run unless `--execute` is supplied:

```console
mac --json admin repo refs prune --repo . --remote origin
mac --json admin repo refs prune --repo . --remote origin --execute --actor operator
```

Execution fails closed unless all of these checks pass:

- The branch is in the exact managed namespace.
- The task exists, is terminal, has no active lease, and has an eligible
  disposition.
- Its grace period has expired.
- A completed branch is proven reachable from the selected canonical base ref.
- GitHub pull-request or GitLab merge-request state was successfully checked
  and no review is open for the branch.
- The remote SHA still equals the SHA observed by audit.

Deletion is one atomic Git push guarded by a per-ref `--force-with-lease` for
the audited SHA. A branch that moves between audit and deletion is retained.
Before the push, MAC records a `requested` evidence tombstone on every task;
after success it records `deleted`. Push failures record `failed`, with
authenticated URLs redacted.

Use repeated `--task` options to limit an audit or prune to selected task IDs.
The JSON report's `counts` and `eligible_count` fields remain useful for
diagnosis and targeted recovery.

## Automatic reconciliation

The MAC hub owns a recurring reconciler. It discovers enabled repositories from
the project-repository registry, resolves and verifies each repository's
`repository_contract.canonical_remote_url`, refreshes its canonical base branch,
and then runs the same audit and exact-SHA prune implementation used by the
manual commands. Spokes and stateless API replicas do not run the reconciler.

Fleet deployment enables `prune` mode on the shared-services-manager (hub) by
default. The first pass starts after five minutes and subsequent passes run
daily. A repository registered with
`metadata.repository_ref_hygiene.enabled=false` is skipped. Automatic `prune`
also requires `canonical_remote_url` in the repository contract; a missing or
mismatched canonical remote fails closed for that repository.

Inspect the scheduler or request an immediate fleet-wide pass through the hub:

```console
mac --json admin repo refs status
mac --json admin repo refs reconcile --mode audit --actor operator
mac --json admin repo refs reconcile --mode prune --actor operator
```

Only an administrator token can trigger a pass. Concurrent scheduled and manual
runs do not overlap: the second request returns `status=busy`. A failure in one
repository is reported but does not prevent later registered repositories from
being evaluated.

The runtime configuration is:

| Variable | Runtime default | Fleet hub default | Meaning |
| --- | --- | --- | --- |
| `MAC_REPOSITORY_REF_RECONCILER_MODE` | `off` | `prune` | `off`, read-only `audit`, or executable `prune` |
| `MAC_REPOSITORY_REF_RECONCILER_INTERVAL_SECONDS` | `86400` | `86400` | Interval from 60 seconds through 7 days |
| `MAC_REPOSITORY_REF_RECONCILER_INITIAL_DELAY_SECONDS` | `300` | `300` | Startup delay from 0 through 86400 seconds |
| `MAC_REPOSITORY_REF_RECONCILER_GRACE_DAYS` | `7` | `7` | Fallback grace for legacy lifecycle records, from 0 through 365 days |
| `MAC_REPOSITORY_REF_RECONCILER_REMOTE` | `origin` | `origin` | Managed Git remote name |
| `MAC_REPOSITORY_REF_RECONCILER_BASE_REF` | auto-detect | auto-detect | Optional `<remote>/<branch>` ancestry target |

Fleet deployment accepts matching `MAC_DEPLOY_REPOSITORY_REF_RECONCILER_*`
overrides. Existing explicit values are preserved on redeploy. Invalid values
disable the reconciler and emit a
`repository.ref.reconciler.configuration_invalid` warning instead of guessing.

Every pass emits a `repository.ref.reconciler.run` observability record with
per-repository classifications, eligible/deleted totals, timestamps, trigger,
and a secret-redacted error when applicable. `mac admin repo refs status` exposes the
active configuration and last report. The hub needs filesystem access to each
registered checkout, Git credentials for its canonical remote, and `gh`
credentials for pull-request verification; missing access blocks deletion for
that repository.

## Lifecycle integration

Task transitions write `metadata.repository_ref_lifecycle` and emit a durable
`repository.ref.lifecycle` control-plane event. Network deletion deliberately
does not occur inside the task transaction: the recurring reconciler performs
Git and GitHub checks later, where failures cannot corrupt task state.

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
