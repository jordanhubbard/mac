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
- [ ] The current `main` source commit is deployed and attested on the hub,
  every configured worker, and every subsequently registered fleet member.
  The earlier `060acc50` receipt was valid for its cohort, but it no longer
  proves this rolling property: Rocky, Natasha, and Bullwinkle remain on
  `47440824` while newer source exists, and none has a current completed
  end-to-end canary.
- [ ] A failed or interrupted fleet deployment can safely resume without
  dispatch-hold drift, credential loss, partial promotion, or manual mutation
  of generated authority files.
  The 2026-09-03 interrupted phase-two incident disproved the general claim:
  `retain_forward` preserved node-side attestation candidates after hub abort
  discarded them, producing split authority. Ledger
  `task_99d9fccdb580449a81095b55a17442de`.
- [ ] Darwin and parallel test harnesses are deterministic enough that required
  gates provide reliable release evidence on the supported development hosts.
- [ ] Database and agent-owned state upgrades are versioned, ordered, recorded,
  and fail closed as specified by ADR 0027.

### Three-agent fleet recovery

The source-first recovery program is tracked by umbrella ledger
`task_216da2b6e43b4316af8f8146e8f711e1`. Detailed incident evidence and proof
criteria live in [Fleet recovery: problems, evidence, and the path to
readiness](problems.md). The approved architecture is
[Three-agent fleet recovery design](superpowers/specs/2026-09-03-three-agent-fleet-recovery-design.md).

- [ ] Worker stdout, stderr, result, and manifest fields are structurally
  redacted before durable persistence; all credentials exposed by the Rocky
  canary are rotated and their predecessor values are rejected. Ledger
  `task_e2dcfa7ebaa14478b0b2d51a45b7d79c`.
- [ ] Fleet credential rotation and deployment derive scoped environment names
  from one canonical immutable fleet identity, so registry key `rocky` and
  runtime name `mac` cannot select different authorities. Ledger
  `task_e2dcfa7ebaa14478b0b2d51a45b7d79c`.
- [ ] Attestation candidates remain inactive on workers until hub promotion,
  interrupted epochs cannot leave node and hub signing authority divergent,
  and stale local journals reconcile to authoritative terminal receipts.
  Ledger `task_99d9fccdb580449a81095b55a17442de`.
- [ ] OpenShell and chat-gateway status fail closed when the named live sandbox
  is absent, stale, or does not match the reported identity; historical
  `active` and `verified` rows are not accepted as runtime proof. Ledger
  `task_f2758902a67c4a11af432c169a1b3923`.
- [ ] Natasha and Bullwinkle each have a recreated, ownership-verified OpenClaw
  sandbox whose sentinel probe passes and whose fresh heartbeat projects
  `health_status=healthy`. Ledger
  `task_f2758902a67c4a11af432c169a1b3923`.
- [ ] Natasha and Bullwinkle each expose at least one verified in-sandbox coding
  route capable of satisfying the normal repository execution contract.
  Ledger `task_f2758902a67c4a11af432c169a1b3923`.
- [ ] A successor release is deployed to Rocky, Natasha, and Bullwinkle, and
  each independently completes a target-pinned canary through claim, execution,
  artifact upload, signature validation, review, completion, and idle return
  without forced completion or broad break-glass.

## CLI and plugin distribution

- [ ] The MAC CLI's known installation, authentication, command-routing, and
  upgrade defects are fixed and covered by supported-host contract tests plus a
  live fleet smoke test.
- [ ] Plugin generation produces a deterministic, versioned, integrity-checked
  artifact whose commands, bundled dependencies, configuration migration, and
  rollback behavior pass installation and upgrade contracts.
- [ ] The generated plugin is verified on every supported host and becomes the
  canonical MAC CLI installation method, with a bounded migration from the
  legacy installer and a tested rollback path.

## Operational autonomy

- [x] Nightly local-news collection reliably publishes one deduplicated report
  to `#localnews`, with delivery and freshness evidence.
  Live receipt `kslug-nightly-news.last-success.json` delivered
  `2026-08-26T22:23:31Z` to `slack:C0AH1QJCT7F`; later runs skipped
  `already_delivered_today`. Launchd job `kslug-nightly-news` (`0 6 * * *`)
  reinstalled on the hub in deploy `20260827T060057Z` at `060acc50`.
- [ ] Scheduled automation fails closed: failed collectors cannot become agent
  prose, repeated unchanged results are suppressed, DMs are allowed, and
  channel broadcasts are limited to the configured destination.
- [ ] The hub agent's proactive channel output is restricted to its configured
  home channel; direct messages to the fleet owner remain allowed.
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

1. Close the Rocky evidence-exposure boundary and complete credential
   rotation after canonical fleet-token naming is fixed.
2. Repair interruption-safe attestation activation and journal reconciliation.
3. Make OpenShell/gateway status truthful, then recreate and verify the two
   missing Linux gateway sandboxes.
4. Restore at least one verified coding route on each Linux worker.
5. Ship a successor release and obtain three independent signed end-to-end
   canary receipts.
6. Prove the hub-mediated upgrade transaction across the full fleet.
7. Close the autonomous review/repair/publication loop with a live `~/Src/mac`
   task.
8. Repair the MAC CLI, verify deterministic plugin generation, and migrate the
   fleet to plugin-based CLI installation.
9. Implement hierarchical sandbox ACL feedback and profile placement.
10. Revisit dream-cycle analysis only after the higher-priority proofs are
   durable.

## Known defects from the v1.3.0 capabilities deck (slide 12)

Source: [MAC capabilities at `d8d491d6`](https://docs.google.com/presentation/d/1yOOzFqRVwhY6opljcPEzfkzQmdjwylsxi1_hFO_8wJ0/edit?slide=id.p12),
captured 2026-08-28. Items stay unchecked until they meet the completion rule
above. Large ADRs are held `--no-dispatch` until hub-verify can land
repository work (`task_321b8e8d`).

- [ ] Default `mac --help` reports the real `mac admin` subcommand count, not
      zero. Ledger `task_2d33cc69`.
- [ ] Current docs no longer describe the deleted Hermes snapshot as present.
      Ledger `task_a0fc238b`. Fate record: `docs/hermes-vendor-fate.md`.
- [ ] Token spend is metered at the router and persisted (ADR 0017). 29.5% of
      `llm.route` events over the seven days to 2026-08-19 recorded no input
      tokens; cost is priced at read time. Ledger `task_b0750aee` (successor to
      failed `task_45927341`).
- [ ] The task view is a graph under progressive disclosure (ADR 0018): the hub
      sends dependency edges so the console can show *what* blocks a task.
      Ledger `task_bdf11b3f`.
- [ ] The coding-route search path is a fleet contract, not per-worker
      environment (ADR 0029). Ledger `task_198bf6ab`.
- [ ] CLI sessions use each harness's hooks for AgentBus inject/record, not
      tmux and not a prompt-level `agentbus wait` (ADR 0032). Ledger
      `task_13a1f7fe`.
- [ ] ADR 0012 (native steward + containerized execution) stays deferred until
      a fleet measurement exists. ADR 0015 already narrowed the containerized
      half to Linux. Ledger `task_3a48fd75`.
- [ ] Hub-verify OpenShell sandboxes receive a dedicated test Postgres DSN
      (not the live hub database) so a repository canary can complete.
      Ledger `task_321b8e8d`. Blocked the v1.3.1 canary `task_d894080c` /
      PR #681.
