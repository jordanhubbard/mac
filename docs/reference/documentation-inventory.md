# Documentation inventory

This generated inventory classifies every Markdown source included in the
documentation tree. Book chapters are authoritative and executable. Runbooks
and references describe production boundaries. Historical material is retained
for provenance and is not a current operating contract.

| Category | Source | Title |
|---|---|---|
| supplemental reference | `activation-probe/calibration-spec.md` | Held-out calibration protocol |
| supplemental reference | `activation-probe/classifier-spec.md` | External activation-probe classifier contract |
| supplemental reference | `activation-probe/integration-guide.md` | External activation-probe worker integration |
| supplemental reference | `activation-probe/prototype-report.md` | External activation-probe prototype |
| supplemental reference | `activation-probe/runtime-selection.md` | External activation capture runtime |
| architecture decision | `adr/0001-unify-hermes-runtime-into-mac.md` | ADR 0001 — Unify the Hermes runtime into the `mac` monorepo |
| architecture decision | `adr/0002-memory-store-at-scale.md` | ADR 0002 — Memory store / vector tier at fleet scale (50–200 agents per hub) |
| architecture decision | `adr/0003-tokenhub-core-into-mac.md` | ADR 0003 — Optional in-mac model router + vault (revisiting TokenHub's boundary) |
| architecture decision | `adr/0004-task-ledger-vs-hermes-kanban.md` | ADR 0004 — One task database: revert the Hermes kanban adoption, keep our own board |
| architecture decision | `adr/0005-elastic-executor-tier-vs-static-fleet.md` | ADR 0005 — Elastic executor tier vs. the static fleet (and "every agent is a GitHub runner") |
| architecture decision | `adr/0006-acp-support.md` | ADR 0006 — Agent Client Protocol (ACP) support |
| architecture decision | `adr/0007-hermes-boundary-mood-nap-soul-memory.md` | ADR 0007 — Per-module ownership: mood_policy, nap_consolidator, soul_snapshot, memory_vetting |
| architecture decision | `adr/0008-openshell-docker-engine-runtime.md` | ADR 0008 - OpenShell uses Docker Engine/Moby as its only container runtime |
| architecture decision | `adr/0009-minimal-base-on-demand-layered-provisioning.md` | ADR 0009 - Minimal sandbox base + on-demand, layered, cached provisioning |
| architecture decision | `adr/0010-fleet-ide-cutover-parity-matrix.md` | ADR 0010 — Fleet IDE Cut-over and Parity Matrix |
| architecture decision | `adr/0011-hub-review-verification-scope.md` | ADR 0011 - Hub review verification uses affected tests |
| architecture decision | `adr/0012-hybrid-native-steward-containerized-execution.md` | ADR 0012 - Native node steward with containerized task execution |
| architecture decision | `adr/0013-authoritative-hub-allocator.md` | ADR 0013 - One authoritative hub allocator |
| architecture decision | `adr/0014-visibility-is-not-a-dispatch-gate.md` | ADR 0014: Agent visibility is a communication boundary, not a dispatch gate |
| architecture decision | `adr/0015-macos-nodes-are-host-installs.md` | ADR 0015: macOS nodes are host installs, not containers |
| architecture decision | `adr/0016-agent-initiated-review.md` | ADR 0016 - Agents decide what a task needs; review is agent-initiated |
| supplemental reference | `agent-lifecycle-proof.md` | Agent Lifecycle Proof |
| historical archive | `archive/field-notes/assessment-2026-08-02.md` | Can MAC do work? — fleet assessment, 2026-08-02 |
| historical archive | `archive/field-notes/assessment-task-1b6783.md` | Assessment: task_1b67831356c347c3a91d782982f47d1c |
| historical archive | `archive/field-notes/assessment-task-21e771-worker3-tailscale-blocker.md` | Assessment: task_21e77194d5fe4fd3963b8b1a61ece9d8 |
| historical archive | `archive/field-notes/assessment-task-7023d6.md` | Assessment: task_7023d6a7ef6e4bbf8f6c2da523a4320f |
| historical archive | `archive/field-notes/assessment-task-83f38e.md` | Assessment: task_83f38e9754f64908a316cccba0952329 |
| historical archive | `archive/field-notes/assessment-task-8bf378.md` | Assessment: task_8bf37845abf445149d99fb4a1e3a41d5 |
| historical archive | `archive/field-notes/assessment-task-97627e.md` | Resolution: task_97627e43b1034100831e726f8981e5e2 |
| historical archive | `archive/field-notes/assessment-task-a33145.md` | Assessment: task_a33145a37db34ffeb55a0db61797df5c |
| historical archive | `archive/field-notes/assessment-task-a608f4.md` | Assessment: task_a608f4405b0446a3b28ed7a8beb4fd65 |
| historical archive | `archive/field-notes/assessment-task-b07fbf.md` | Assessment: task_b07fbff6994e41a39ce24157f1832ad5 |
| historical archive | `archive/field-notes/assessment-task-de3502.md` | Assessment: task_de35029099d34c94be186c8992ee706a |
| historical archive | `archive/field-notes/assessment-task-f6a813.md` | Assessment: task_f6a813fede7841d28b154af3a544864a |
| historical archive | `archive/field-notes/assessment-task-f9cd72.md` | Assessment: task_f9cd72342aef4e7b8701b131b12d29ff |
| historical archive | `archive/field-notes/closeout-dreamrepair-3dc2cf-openclaw-fleet-rollout.md` | Close-Out: dream finding `dreamrepair:3dc2cf317ea21e032952a355c3550f88` (openclaw_fleet_rollout deliverable) |
| historical archive | `archive/field-notes/closeout-dreamrepair-5404b15-skill.md` | Close-Out: dream finding `dreamrepair:5404b15fffa355d739c21e138c5cc122` (skill subsystem) |
| historical archive | `archive/field-notes/closeout-dreamrepair-965c6e89-openclaw-fleet-rollout.md` | Close-Out: dream finding `dreamrepair:965c6e89c762d29f07df25aafd3ac96f` (openclaw_fleet_rollout deliverable) |
| historical archive | `archive/field-notes/closeout-review-finalize-verify-prerequisite.md` | Close-Out: dream-finding review finalize/verify prerequisite |
| historical archive | `archive/field-notes/closeout-task-9c83aa5b-skill.md` | Close-Out: low-confidence dream finding `skill` (parent `task_9c83aa5b`) — CLOSE, NOT ACTIONABLE |
| historical archive | `archive/field-notes/contract-verify-environment-failure-finding.md` | Contract-verify environment failure investigation finding |
| historical archive | `archive/field-notes/crash-incident-finding.md` | Crash incident investigation finding |
| historical archive | `archive/field-notes/disposition-dreamrepair-c8dd8037-skill.md` | Disposition: dream finding `dreamrepair:c8dd80378a16692ba4e0cd5ef57f2bf1` (skill subsystem) |
| historical archive | `archive/field-notes/disposition-dreamrepair-ecd3548-skill.md` | Disposition: low-confidence dream finding `skill` (`dreamrepair:ecd3548120e07c38d04e46f0c62e16dd`) — CLOSE, NOT ACTIONABLE |
| historical archive | `archive/field-notes/disposition-hgx-session-c0b2f9fd4e0b.md` | Disposition: HGX session `c0b2f9fd4e0b` — workspace/PVC inventory, preservation, and convert-in-place feasibility |
| historical archive | `archive/field-notes/disposition-hgx-session-c902fab4d55f.md` | Disposition: HGX session `c902fab4d55f` — workspace/PVC inventory, preservation, and convert-in-place feasibility |
| historical archive | `archive/field-notes/disposition-task-394db89d-slack.md` | Disposition: low-confidence dream finding `slack` (`dreamrepair:394db89d377ef58abf97ace7d54d728c`) — not actionable |
| historical archive | `archive/field-notes/disposition-task-46eb6c-skill-env-prereqs.md` | Disposition: skill environment-prerequisite finding — smallest repair applied |
| historical archive | `archive/field-notes/disposition-task-9c83aa5b-skill.md` | Disposition: low-confidence dream finding `skill` (parent `task_9c83aa5b`) — not actionable |
| historical archive | `archive/field-notes/disposition-task-c3a30819-skill.md` | Disposition: low-confidence dream finding `skill` (`dreamrepair:828e1ef4a530935a9a7db4b1807202e1`) — not actionable |
| historical archive | `archive/field-notes/disposition-task-cc1dedb0-slack.md` | Disposition: low-confidence dream finding `slack` (`dreamrepair:cc1dedb0d3036d289aafc1e42b4a22aa`) — not actionable |
| historical archive | `archive/field-notes/dream-finding-3dc2cf.md` | Dream-Finding Assessment: dreamrepair:3dc2cf317ea21e032952a355c3550f88 |
| historical archive | `archive/field-notes/dream-finding-58afe2-openclaw-entrypoint-ready-token.md` | Dream-Finding Assessment: dreamrepair:58afe279d34e186ee4d6d6125532371c |
| historical archive | `archive/field-notes/dream-finding-6d1b5b.md` | Dream-Finding Assessment: dreamrepair:6d1b5bbe0a13515fef0bd061ef001119 |
| historical archive | `archive/field-notes/dream-finding-805aed7.md` | Dream-Finding Assessment: dreamrepair:805aed758e12f0f95cf0c3dbf39811ce |
| historical archive | `archive/field-notes/dream-finding-965c6e89.md` | Ground Truth: dream finding `dreamrepair:965c6e89c762d29f07df25aafd3ac96f` (openclaw_fleet_rollout deliverable) |
| historical archive | `archive/field-notes/dream-stalled-finalizer-recovery-finding.md` | Dream-repair review finding: stalled-finalizer recovery |
| historical archive | `archive/field-notes/dream-triage-828e1ef4.md` | Triage: Dream Finding `dreamrepair:828e1ef4a530935a9a7db4b1807202e1` |
| historical archive | `archive/field-notes/findings-crash-1fc349e1-startup-selftest-attestation-gap.md` | Findings: startup self-test attestation-gap crash (crash_1fc349e109ed4ff9885acf1c8ba99948) |
| historical archive | `archive/field-notes/findings-crash-591645a3-startup-selftest-timeout.md` | Findings: startup self-test transient-timeout crash (crash_591645a352fc4d54bf5e3f99384da7dc) |
| historical archive | `archive/field-notes/findings-crash-mac-agent-service-62021be0.md` | Findings: mac-agent-service startup self-test crash (62021be0) |
| historical archive | `archive/field-notes/findings-crash-service-unknown-crash-path.md` | Findings: crash_service "unknown" crash path (ground truth for parent P0 crash-repair) |
| historical archive | `archive/field-notes/fleet-ide-workbench-plan.md` | Fleet Workbench — Clean-Slate IDE Plan |
| historical archive | `archive/field-notes/forensics-task-643b33ee1c7b4a4ab7a81bf8d5af34a4.md` | Forensics: Diagnose 90s Dispatch Delay for CLI-Created Probe Task |
| historical archive | `archive/field-notes/forensics-task-a32a35e90ab0434e8c7766057b268bc6.md` | Root-Cause Report: Silent Executor Insta-Block for task_a32a35e90ab0434e8c7766057b268bc6 |
| historical archive | `archive/field-notes/haskell_migration.md` | A Notional Haskell Migration Plan for MAC |
| historical archive | `archive/field-notes/investigation-dream-skill-generic-area-bucket.md` | Investigation: dream finding with a generic `skill` affected label — placeholder area bucket, not a defect location |
| historical archive | `archive/field-notes/investigation-dream-skill-tool_or_skill_name-actionability.md` | Investigation: dream `tool_or_skill_name` (skill) finding — actionability & root signal |
| historical archive | `archive/field-notes/investigation-dream-tests-generic-area-bucket.md` | Investigation: dream finding `dreamrepair:173ce952` with a generic `tests` affected label — placeholder area bucket, not a defect location |
| historical archive | `archive/field-notes/investigation-dreamrepair-394db89d-slack.md` | Ground Truth: dream finding `dreamrepair:394db89d377ef58abf97ace7d54d728c` (slack failure_pattern) |
| historical archive | `archive/field-notes/investigation-dreamrepair-477446f5-slack.md` | Ground Truth: dream finding `dreamrepair:477446f5c8b8bf1972f2ad31444c956b` (slack failure_pattern) |
| historical archive | `archive/field-notes/investigation-dreamrepair-4c4429b-scripts-openclaw.md` | Investigation: dream finding `dreamrepair:4c4429bc` (scripts / openclaw_fleet_rollout audit) |
| historical archive | `archive/field-notes/investigation-dreamrepair-5404b15-skill.md` | Investigation: dream finding `dreamrepair:5404b15fffa355d739c21e138c5cc122` (skill subsystem) |
| historical archive | `archive/field-notes/investigation-dreamrepair-71b00e8-slack.md` | Ground Truth: dream finding `dreamrepair:71b00e8122761c2caeacd04c7ed3f49c` (slack display-label trace) |
| historical archive | `archive/field-notes/investigation-dreamrepair-c8dd8037-skill.md` | Investigation: dream finding `dreamrepair:c8dd80378a16692ba4e0cd5ef57f2bf1` (skill subsystem) |
| historical archive | `archive/field-notes/investigation-dreamrepair-cc1dedb0-slack.md` | Ground Truth: dream finding `dreamrepair:cc1dedb0d3036d289aafc1e42b4a22aa` (slack failure_pattern) |
| historical archive | `archive/field-notes/investigation-dreamrepair-d94ad78-skill.md` | Investigation: dream finding `dreamrepair:d94ad78027c32d4825923f0ba91e9497` (skill) |
| historical archive | `archive/field-notes/investigation-dreamrepair-da0ac0f3-slack.md` | Ground Truth: dream finding `dreamrepair:da0ac0f3cab187290c91e5b26a6c5b9f` (slack failure_pattern) |
| historical archive | `archive/field-notes/investigation-dreamrepair-ffbc63f8-skill.md` | Investigation: dream finding `dreamrepair:ffbc63f8695e9316b064bb1f6d3566cb` (skill) |
| historical archive | `archive/field-notes/investigation-predispatch-conflict-5a43ad.md` | Investigation: `predispatch_conflict.py` failure-pattern (dream finding) |
| historical archive | `archive/field-notes/investigation-review-finalize-verify-prerequisite.md` | Investigation: dream-finding review finalize/verify prerequisite ground truth |
| historical archive | `archive/field-notes/investigation-task-2284f3-env-prerequisite-false-positive.md` | Investigation: mac environment-prerequisite finding is a classifier false positive |
| historical archive | `archive/field-notes/investigation-task-b6ddd8-skill-env-prereqs.md` | Investigation: skills environment-prerequisite behavior vs. the finding |
| historical archive | `archive/field-notes/investigation-task-ed7b0b-new-file-staging-finalizer.md` | Investigation: new-file staging finalizer ground truth (task_ed7b0b) |
| historical archive | `archive/field-notes/investigation-task-f869c0-new-file-staging-finalizer.md` | Investigation: new-file staging finalizer ground truth (task_f869c0) |
| historical archive | `archive/field-notes/job-per-task-roles-spec-review.md` | Review: docs/job-per-task-roles-spec.md |
| historical archive | `archive/field-notes/job-per-task-roles-spec.md` | Job-per-task Role Specialisation — Design Spec |
| historical archive | `archive/field-notes/k8s-native-rewrite-plan.md` | Kubernetes-native rewrite plan |
| historical archive | `archive/field-notes/linear-bridge-spec-review.md` | Linear Bridge Spec — Review Notes |
| historical archive | `archive/field-notes/linear-bridge-spec.md` | Linear Bridge — Design Spec |
| historical archive | `archive/field-notes/mac-task-bd-parity-audit.md` | `mac task` ↔ `bd` (beads) functional-parity audit |
| historical archive | `archive/field-notes/metadata-sync-assessment.md` | Metadata sync assessment (post-bd-bridge) |
| historical archive | `archive/field-notes/prereq-task-029665.md` | Preflight: HGX auth path, fleet baseline, and standard-dind fungible reference |
| historical archive | `archive/field-notes/prereq-task-403ed263.md` | Prerequisite Verification: task_403ed263ed7e45c6b7624345005a097c |
| historical archive | `archive/field-notes/prereq-task-e94f546c.md` | Prerequisite Investigation: task_e94f546cf9dc41409d4a9fe6b8b39dcd |
| historical archive | `archive/field-notes/prereq-task-fd2f34.md` | Prerequisite Investigation: task_fd2f34b64823410c84a14fc0345610ff |
| historical archive | `archive/field-notes/provenance-dreamrepair-77fc3e59-slack.md` | Provenance: low-confidence dream finding `slack` (`dreamrepair:77fc3e59014ba0d7950d22387f0204a0`) — self-referential evidence chain, no concrete defect |
| historical archive | `archive/field-notes/quickstart-gap-analysis.md` | Quickstart Gap Analysis |
| historical archive | `archive/field-notes/replace-hgx-session-c902fab4d55f.md` | Field note: HGX session `c902fab4d55f` — realize capacity via preserve-nothing + REPLACE |
| historical archive | `archive/field-notes/scaling-plan.md` | Scaling Plan |
| historical archive | `archive/index.md` | Historical archive |
| supplemental reference | `audit.md` | MAC Codebase Audit — Active Code, Duplication & Accretion |
| supplemental reference | `authority-boundary.md` | Who owns which authority question |
| book | `book/01-system.md` | MAC as a System |
| book | `book/02-local-start.md` | Install and Start Locally |
| book | `book/03-projects-and-tasks.md` | Projects and Tasks |
| book | `book/04-machines-and-agents.md` | Machines and Agents |
| book | `book/05-evidence-review-completion.md` | Evidence, Review, and Completion |
| book | `book/06-hermes-and-ide.md` | Hermes and the Fleet IDE |
| book | `book/07-repository-contracts.md` | Repository Contracts |
| book | `book/08-plans-and-dags.md` | Plans and Task DAGs |
| book | `book/09-fleet-onboarding.md` | Heterogeneous Fleet Onboarding |
| book | `book/10-identity-and-secrets.md` | Identity, Credentials, and Secrets |
| book | `book/11-publication-and-refs.md` | Review, Publication, and Ref Hygiene |
| book | `book/12-operations.md` | Operating the Fleet |
| book | `book/13-deployment-topologies.md` | Deployment Topologies |
| book | `book/14-images-and-cutover.md` | Qualified Images and Synchronized Cutover |
| book | `book/15-sandboxed-runtimes.md` | Sandboxed Agent Runtimes |
| book | `book/16-apis-and-integrations.md` | APIs, AgentBus, and Integrations |
| book | `book/17-learning-evals-scaling.md` | Learning, Evals, and Scaling |
| book | `book/18-capstone.md` | From Request to Production |
| runbook | `break-glass-host-recovery.md` | Break-glass host recovery |
| supplemental reference | `c26-certifier-phase-profile-example.md` | c26 certifier phase-profile example |
| supplemental reference | `client-bootstrap-contract.md` | SSH Client Bootstrap Contracts |
| supplemental reference | `coding-cli-credentials.md` | Coding-CLI Credentials and Model Selection |
| supplemental reference | `crash-diagnosis-and-repair.md` | Crash diagnosis and autonomous repair |
| supplemental reference | `dashboard-connection.md` | Dashboard Connection Contract |
| supplemental reference | `deploy-prerequisite-vs-phase1-audit.md` | Audit: prove deploy prerequisites before phase-1 mutation, preserve Python diagnostics |
| supplemental reference | `dispatch-priority-bias-audit.md` | Dispatch priority bias ordering audit |
| supplemental reference | `dreaming-rewrite.md` | Dreaming, rewritten |
| supplemental reference | `env-config-reference.md` | MAC environment configuration reference |
| runbook | `fleet-cutover-transaction-protocol.md` | Fleet Cut-over Transaction Protocol |
| supplemental reference | `fleet-directives.md` | Fleet directives |
| runbook | `fleet-node-onboarding-checklist.md` | Fleet node onboarding checklist |
| supplemental reference | `fleet-operational-learning.md` | Fleet operational learning |
| supplemental reference | `fleet-registry-schema.md` | Fleet registry schema |
| supplemental reference | `getting-started.md` | MAC Quickstart |
| supplemental reference | `guide/01-architecture.md` | System Architecture |
| supplemental reference | `guide/02-getting-started.md` | Getting Started |
| supplemental reference | `guide/03-advanced.md` | Advanced Concepts |
| supplemental reference | `guide/04-ui.md` | The UI |
| supplemental reference | `guide/05-developer-guide.md` | Developer Guide |
| supplemental reference | `guide/README.md` | mac documentation |
| supplemental reference | `hermes-boundary.md` | Hermes Boundary |
| supplemental reference | `hermes-integration.md` | Hermes Integration |
| supplemental reference | `hermes-retirement-premises.md` | Testing the premises for retiring the vendored Hermes tree |
| supplemental reference | `hermes-vendor-fate.md` | Fate of the vendored Hermes tree |
| supplemental reference | `hgx-elastic-capacity.md` | HGX elastic capacity |
| supplemental reference | `home-consolidation.md` | Home-Directory Consolidation: Analysis & Plan |
| runbook | `hub-availability.md` | Hub Availability |
| supplemental reference | `hub-host-saturation-remediation.md` | Hub-Host Saturation Remediation |
| supplemental reference | `human-interface-selector.md` | The human interface: support both, activate one |
| supplemental reference | `image-publication-and-qualification.md` | Image Publication and Pre-Publication Qualification |
| supplemental reference | `in-flight-agent-messages.md` | Reaching an agent that is already working |
| landing page | `index.md` | MAC: trustworthy work across an agent fleet |
| supplemental reference | `integration-authority-contract.md` | Integration Authority Contract |
| supplemental reference | `memory-tier-schema.md` | MAC vector memory tier — schema, collections, model, TTLs |
| supplemental reference | `memory-tier-verification.md` | Memory tier — end-to-end verification |
| supplemental reference | `notifier-configuration-guide.md` | Notifier Configuration Guide |
| supplemental reference | `oneshot-isolation-gate-verification.md` | Oneshot isolation — contract gate verification |
| supplemental reference | `openclaw-identities.md` | OpenClaw public identities and fleet representation |
| supplemental reference | `openshell-nemo-relay-e2e.md` | OpenShell + NeMo Relay: container-contract verification |
| supplemental reference | `openshell-nemo-relay-integration.md` | OpenShell + NeMo Relay integration |
| supplemental reference | `openshell-sandbox.md` | Running Hermes under the OpenShell sandbox |
| runbook | `production-deployment.md` | Production Deployment |
| generated reference | `reference/cli.md` | Command-line reference |
| generated reference | `reference/documentation-inventory.md` | Documentation inventory |
| generated reference | `reference/openapi.md` | HTTP API reference |
| generated reference | `reference/staged-module-integration-audit.md` | Staged-but-unwired `src/mac` module integration audit |
| supplemental reference | `repository-cicd-monitor.md` | Repository CI/CD lifecycle monitoring |
| supplemental reference | `repository-ref-hygiene.md` | Managed Repository Ref Hygiene |
| supplemental reference | `repository-runtime-contract.md` | Repository Runtime Contract |
| supplemental reference | `review-strategy-experiments.md` | Review-strategy experiments |
| supplemental reference | `scientific-optimizer.md` | Autonomous scientific optimizer |
| supplemental reference | `secrets-management-guide.md` | Secrets Management Guide |
| supplemental reference | `security/openshell-0.0.72-compatibility-review.mdx` | OpenShell 0.0.72 Compatibility Review |
| runbook | `soul-preservation-runbook.md` | Soul Preservation Runbook |
| supplemental reference | `structured-task-bodies.md` | Structured task bodies: actions on a Component |
| historical archive | `superpowers/plans/2026-05-31-autonomous-project-routing-review-fix-loop.md` | Autonomous Project Routing and Review/Fix Loop Implementation Plan |
| historical archive | `superpowers/specs/2026-05-31-autonomous-review-fix-loop-design.md` | Autonomous Project Routing and Review/Fix Loop Design |
| historical archive | `superpowers/specs/2026-06-04-k8s-bootstrap-fleet-registration-design.md` | K8s bootstrap fleet registration — design |
| runbook | `synchronized-fleet-cutover.md` | Synchronized Fleet Cut-over |
| supplemental reference | `task-dependency-semantics.md` | Task dependency failure semantics |
| supplemental reference | `task-throughput-observability.md` | Task throughput observability |
| supplemental reference | `testing-strategy.md` | Test portfolio strategy |
