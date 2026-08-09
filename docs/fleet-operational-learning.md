# Fleet operational learning

Status: implemented, tested, and deployed, 2026-06-30.

## Decision

Operational outcomes are control inputs, not merely log lines or prompt
context. When one fleet member proves a repository-access pattern works, MAC
records that success in common memory and prefers it for later work. When an
agent proves a pattern does not work, MAC records the failure, temporarily
removes that agent from equivalent routing, and tries a known-successful peer
instead of repeating the same operation indefinitely.

The first enforced application is repository access during autonomous review.
The design is intentionally reusable for other operational capabilities.

## Repository-access learning record

Repository access outcomes use the existing durable `memory_records` store.
Each record has:

- `record_type=fleet_learning:repository_access`;
- `subject_type=agent` and `subject_id=<agent-id>`;
- JSON content with schema `mac.fleet_learning.v1`;
- project, repository host, operation, agent, credential source name, outcome,
  failure class, bounded redacted error signature, recommendation, task and
  review identifiers, and timestamp.

Credential values are never stored. A source such as `env:GH_TOKEN`,
`env:GITHUB_TOKEN`, `ssh-agent-or-key`, or `ambient:https` describes the
mechanism only. Remote URLs and error text are redacted before persistence.

These records are common fleet memory: every reviewer-selection pass can read
them, the vector tier can embed them, and executors can recall relevant records
in their task prompt.

## Runtime behavior

### Review repository access

Review clones use the same environment-backed authentication resolver as task
fetch and publication. HTTPS credentials may be injected into the individual
Git command but must not remain in the review checkout's `origin` URL or any
evidence artifact. Kubernetes review jobs receive optional Git-host token
environment variables but not MAC's general secret-encryption key.

Every completed remote preparation writes one success or failure learning.
Authentication and authorization failures trigger an immediate default-review
workflow tick after the memory write.

The control plane's pushed-ref evidence check uses the same authentication
resolver. A control-plane authentication, authorization, or network failure is
treated as indeterminate rather than as proof that a pushed ref is absent; the
independently routed reviewer remains responsible for verification. A
successful lookup with no matching ref still rejects phantom-push evidence.

### Success-first reviewer routing

For the task's repository host, reviewer candidates are ordered as follows:

1. agents with a recent successful `review_clone` learning;
2. agents with no recent matching learning.

An agent whose newest matching learning is an authentication or authorization
failure is ineligible during the failure cooldown. A later success immediately
supersedes the failure. After the cooldown, the agent becomes unknown rather
than permanently banned, so credential repairs can be proven naturally.

Configuration:

- `MAC_REPOSITORY_ACCESS_FAILURE_COOLDOWN_SECONDS` defaults to 1800;
- `MAC_REPOSITORY_ACCESS_SUCCESS_TTL_SECONDS` defaults to 86400.

### Bounded retries

Review nudge attempts are counted from durable delivered nudge messages for
the specific review. Idempotent claim calls are not an attempt counter. Once
`MAC_REVIEW_NUDGE_MAX_ATTEMPTS` is reached, the review is retracted through the
existing bounded review workflow instead of producing an unbounded retry
storm.

### Prompt recall

Structured fleet learnings are recalled alongside project deployment lessons.
The prompt shows the outcome and actionable recommendation, not credentials or
raw command output. Deterministic routing remains authoritative; prompt recall
helps agents apply the same lesson inside their implementation work.

## Operator inspection and recovery

Read the records through the MAC API/CLI rather than opening the database or
editing `.oompah/tasks`/task files:

```console
mac --json admin memory search \
  --record-type fleet_learning:repository_access \
  --order desc --limit 50

mac --json admin memory search \
  --subject-type agent --subject-id agent_... \
  --record-type fleet_learning:repository_access \
  --order desc --limit 20

mac --json task show task_...
```

The `content` field is JSON text. Relevant routing fields are `project`,
`repository_host`, `operation`, `agent_id`, `credential_source`, `outcome`,
`failure_class`, and `at`. Multiple successes are normal when a reviewer is
nudged more than once; the newest matching record controls routing.

For credential repair, update the agent's host-specific environment or the
Kubernetes runner Secret, restart/redeploy the affected runtime, and let a real
Git operation write the superseding success. Do not fabricate a success memory
record. A vault record alone does not populate `GH_TOKEN`/`GITEA_TOKEN` in a
worker process.

Review status distinguishes repository access from later execution:

- `reviewer_repository_access_authentication` or `_authorization` is a learned
  Git access failure and affects reviewer eligibility.
- `reviewer_unable_to_produce_verdict_after_<N>_attempts` means the repository
  may have cloned successfully, but the reviewer executor failed to emit a
  valid signed verdict. That failure is bounded and visible, but it is not
  currently converted into a repository-access failure or a general
  reviewer-harness routing exclusion.

## Acceptance Criteria

- [x] Review jobs receive optional Git-host credentials while `MAC_SECRET_KEY`
  remains absent.
- [x] A private HTTPS review clone uses environment-backed authentication and
  leaves a credential-free `origin` URL and evidence context.
- [x] Review clone success and failure both create secret-free
  `mac.fleet_learning.v1` records.
- [x] A recent authentication failure causes the pending review to be
  retracted and reassigned to a reviewer with a recent success when one exists.
- [x] A later success or elapsed cooldown restores reviewer eligibility.
- [x] Delivered nudge attempts hit the configured cap even when review claims
  are idempotent.
- [x] Executor prompts include relevant structured fleet-learning
  recommendations.
- [x] Pushed-ref verification reuses environment-backed Git authentication,
  redacts credentials, and does not misclassify a verifier auth failure as a
  missing ref.
- [x] Focused tests and `scripts/run-contract-tests.sh` pass.
- [x] CodeGraph reports a passing affected-code audit for all changed source
  files.

Implementation tracking: `task_f16df80ee0b4404091fa9f86fcba64da` (completed).
Core implementation commits: `7cf4e3d` and `7503ba0`.
