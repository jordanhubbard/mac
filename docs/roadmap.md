# MAC Roadmap

MAC's primary mission is to make `~/Src/mac` capable of completing useful
software work autonomously: select and route work, execute it in least-privilege
sandboxes, review and repair its own changes, publish approved results, and
upgrade the fleet without requiring an operator to perform the mechanics.

## Completion rule

A checkbox may be checked only when the item is:

1. code-complete on `main`;
2. covered by the repository's required gates;
3. deployed to the entire configured fleet; and
4. verified against the live fleet with durable evidence.

Merged, tested, staged, canaried, or partially deployed work remains unchecked.
Each checked item must include the verifying source commit and deployment
transaction or equivalent durable evidence.

## Autonomous work loop

- [ ] The hub can produce an honest live status report of autonomous throughput,
  blocked work, ineffective attempts, review backlog, and worker health.
- [ ] Project work is selected and routed from durable project metadata without
  requiring a human to choose a worker or low-level execution capability.
- [ ] Every coding task executes in an isolated least-privilege sandbox with an
  inspectable ACL path and no authority beyond its parent ACL scope.
- [ ] Sandbox profiles compose deterministically as a nested Windows/POSIX-style
  ACL hierarchy with explicit deny, transitive group/role inheritance, and a
  documented path-resolution rule.
- [ ] ACL-denial feedback can choose a better parent profile or add a narrower
  child layer without flattening or widening the parent scope.
- [ ] ACL-denial evidence identifies the denied property and exact profile layer,
  recommends either a bounded additive child ACL or placement at an existing
  parent, and proves that the recommendation cannot exceed parent authority.
- [ ] Speculative work starts at the zero-privilege leaf and is dynamically
  placed at the narrowest profile that satisfies observed requirements.
- [ ] Successful work produces signed evidence, a reviewable branch, and a pull
  request or merge request.
- [ ] Review findings are structured, fed into the next coding attempt, and
  iterated autonomously until approval or a bounded terminal failure.
- [ ] Approved work is published and the durable task is completed without a
  human copying state between systems.
- [ ] AgentBus is the authoritative transport control plane for all autonomous
  work, including directives, worker traffic, status, and recovery.
- [ ] Every managed OpenShell policy allows egress to exactly its configured
  AgentBus hub endpoint and port, with no broad network exception.
- [ ] PostgreSQL is the authoritative MAC control-plane store; control-plane
  validation, recovery, and rollout gates do not silently fall back to SQLite.
- [ ] A live end-to-end proof shows MAC selecting, implementing, reviewing,
  repairing, publishing, and completing nontrivial work in `~/Src/mac`.

## Fleet and self-upgrade

- [ ] OpenClaw can submit authenticated human upgrade intent without receiving
  deployment authority.
- [ ] An admin can issue an `upgrade yourself` or critical roll-forward request
  through OpenClaw, and OpenClaw can report durable progress and outcome.
- [ ] Upgrade credentials are independently revocable, human-bound,
  least-privilege, and projected through the fenced secret channel; neither
  OpenClaw nor the supervisor receives general deploy or keystore authority.
- [ ] Keystore operations are scope-aware and auditable so each service can read
  only the named credentials required for its finite role.
- [ ] The hub accepts only approved immutable releases with remote CI and local
  contract-test evidence.
- [ ] A host-native supervisor can swap the hub generation, prove health, and
  roll back through a finite transaction without an LLM, arbitrary coding, or
  arbitrary command execution.
- [ ] Critical hub recovery has a break-glass path that remains usable when the
  current hub or OpenClaw generation is stale, while preserving authorization,
  audit, health proof, and rollback.
- [ ] The restarted hub resumes the durable release epoch and rolls workers
  forward or back by bounded cohorts.
- [ ] Crash recovery, authorization failures, failed health proofs, and cohort
  rollback are covered and proven in the live fleet.
- [ ] The current `main` source commit is deployed and attested on Rocky,
  Natasha, Bullwinkle, and every subsequently registered fleet member.
- [ ] A failed or interrupted fleet deployment can safely resume without
  dispatch-hold drift, credential loss, partial promotion, or manual mutation
  of generated authority files.
- [ ] Darwin and parallel test harnesses are deterministic enough that required
  gates provide reliable release evidence on the supported development hosts.
- [ ] Database and agent-owned state upgrades are versioned, ordered, recorded,
  and fail closed as specified by ADR 0027.

## Operational autonomy

- [ ] Nightly local-news collection reliably publishes one deduplicated report
  to `#localnews`, with delivery and freshness evidence.
- [ ] Scheduled automation fails closed: failed collectors cannot become agent
  prose, repeated unchanged results are suppressed, DMs are allowed, and
  channel broadcasts are limited to the configured destination.
- [ ] Rocky's proactive channel output is restricted to `#rockyandfriends`;
  direct messages to the fleet owner remain allowed.
- [ ] OpenClaw gateway shutdown and deployment checkpoints handle WAL-backed
  state, bounded subprocess shutdown, and the configured OpenShell endpoint
  without corrupting or abandoning a rollout.
- [ ] Fleet configuration and credential environment files are written
  atomically and preserve real line boundaries, permissions, and scoped token
  names across retries.
- [ ] Dream-cycle analysis uses a current authoritative data source and produces
  useful, deduplicated output. This is a nice-to-have and must not block core
  autonomous-work or fleet-upgrade milestones.

## Near-term order

1. Complete and verify the top-of-tree Rocky rollout.
2. Restore the nightly `#localnews` report.
3. Prove the hub-mediated upgrade transaction across the full fleet.
4. Close the autonomous review/repair/publication loop with a live `~/Src/mac`
   task.
5. Implement hierarchical sandbox ACL feedback and profile placement.
6. Revisit dream-cycle analysis only after the higher-priority proofs are
   durable.
