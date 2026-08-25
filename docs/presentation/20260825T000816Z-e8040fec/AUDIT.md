# Audit — every claim in this deck, traced to source

Audited at commit `e8040fec` on 2026-08-25T00:08:16Z, by reading the tree
directly. Generated references (`docs/reference/cli.md`,
`docs/reference/openapi.md`) are treated as authoritative for surface counts
because CI fails when they drift from the live parser and OpenAPI schema.

**Where the README and the code disagree, this audit follows the code**, and
says so.

The previous capabilities deck (`8b424c20`, 2026-08-20) is not updated. This
directory is a sibling, pinned to this SHA.

---

## 0. Current-doc pass

`docs/reference/documentation-inventory.md` at this commit lists every
Markdown file under `docs/` except pinned decks. Each row got a
changed / not-changed decision against the tree. The summary:

| Class in the inventory | Decision | Source anchor |
|---|---|---|
| generated reference (`cli.md`, `openapi.md`, env registry, this inventory) | **not-changed** | `Makefile` `docs-build` runs `scripts/generate-docs-reference.py --check`; CI Documentation on `e8040fec` succeeded |
| architecture decision `docs/adr/0001`–`0031` | **not-changed as files**; statuses recorded in §7 | status line of each ADR |
| book chapters `book/01`–`book/18` | **not-changed** | `mkdocs.yml` `nav:`; `make docs-check` executes every shell example |
| guide `docs/guide/` | **not-changed** | `tests/test_guide_docs_are_true.py` |
| historical archive | **not current behaviour** | `docs/archive/index.md` label; inventory category `historical archive` |
| remaining current supplemental / runbook docs | **not-changed**, except the `_hermes` prose listed in §8 | tree vs each file; `_hermes` grep of current docs |

The full per-file table is §11. No current inventory row was left unchecked.

---

## 1. Counts

| Claim | How it was obtained |
|---|---|
| 205,258 lines of Python under `src/` | `find src -name "*.py" \| xargs wc -l` at `e8040fec` |
| 212 modules in `src/mac` | `ls src/mac/*.py \| wc -l` |
| 506 test files | `ls tests/*.py \| wc -l` |
| 414 HTTP routes | `grep -cE "^\| \`(GET\|POST\|PUT\|PATCH\|DELETE)" docs/reference/openapi.md` |
| 125 CLI verbs | Parsed from `docs/reference/cli.md`: task 45, project 9, agent 17, admin 54 |
| 18 book chapters | `nav:` in `mkdocs.yml`, `book/01`–`book/18` |
| 31 ADRs | `docs/adr/0001`–`0031`; 16 are Proposed |
| 5 coding-agent routes | `src/mac/coding_agent.py` `AGENT_PRIORITY`: `opencode`, `pi`, `claude`, `codex`, `cursor` |
| 129 commits since `v1.1.0` | `git rev-list --count v1.1.0..e8040fec` |

The generated `mac --help` text says "0 administrative commands live under
`mac admin`". The same generated `docs/reference/cli.md` then lists 54 admin
groups, including `judgement`. The deck uses 54, not 0. Recorded as a
generator defect in §8.

---

## 2. The object model (diagram 01)

| Claim | Source |
|---|---|
| The CLI is organised around project / task / agent / admin | `docs/reference/cli.md`, `mac --help`: "The objects mac models. Start here: project … task … agent" |
| project / task / agent blurbs | `mac --help` verbatim in `docs/reference/cli.md` |
| Project dispatch pause is separate from per-task staging | `README.md` Core Contracts |
| Recovery verbs | `mac task --help`: `recover-stranded`, `recover-finalizer`, `recover-stalled-finalizer` |
| Break-glass is grantable/listable/revocable | `mac task --help` |
| Visibility is not a dispatch gate | `docs/adr/0014-visibility-is-not-a-dispatch-gate.md` |
| `judgement` is an admin group | `docs/reference/cli.md` Getting work done: `judgement` — "hourly process-quality authority over lifecycle gates" |
| Generated CLI reference is checked | `scripts/generate-docs-reference.py`; `Makefile` `docs-build --check` |
| MCP server is a client, not a second implementation | commit `efb428e9` |
| Python client is contract-checked against the hub | `tests/test_dispatch_route_contract.py` |
| ACP and A2A are implemented specifications | `README.md` lineage; `/.well-known/acp`, `/.well-known/agent-card.json`, `/a2a` in `docs/reference/openapi.md` |
| `/ui` is read-only observability | `docs/adr/0025-the-hub-ui-is-the-observability-console.md` |
| 125 verbs, 414 routes | §1 |

---

## 3. Task lifecycle (diagram 02)

| Claim | Source |
|---|---|
| Twelve states | `src/mac/models.py` `class TaskState`: open, waiting, blocked, claimed, running, needs_review, needs_input, **stopped**, reviewing, completed, failed, cancelled |
| `STOPPED` is not terminal | same file: "NOT terminal: stopped work is live work"; allocator only considers OPEN |
| Three terminal states | `TERMINAL_TASK_STATES` = completed, failed, cancelled |
| Non-terminal work is the default list view | `ACTIVE_TASK_STATES` comment in `src/mac/models.py` |
| Hub still drives RUNNING → NEEDS_REVIEW → REVIEWING | `src/mac/services.py` `advance_default_review_workflow`; ADR 0016 is **Accepted** as a decision (2026-08-20) and is not yet the runtime |
| Native merge queue, "never land an untested tree" | `src/mac/native_merge_queue.py`; commit `2b49fb23` |
| Failure causes are classified | `src/mac/review_failure_classifier.py`, `src/mac/attempt_failure_classifier.py` |

Eleven became twelve since the `8b424c20` deck: `STOPPED` was added to
`TaskState`. The live hub at capture had 4 tasks in that state (`mac task
stats`, 2026-08-25).

---

## 4. Coordination (diagram 03)

Unchanged in substance from the `8b424c20` audit. AgentBus is still a
broadcast bus (`src/mac/agentbus_control.py`). Lifecycle verbs remain
stand_down, abort, pause, resume, status. `abort` is still the destructive
set. Inbox consumption for a CLI session that has no background slot is
still `drain` / `pending` (`src/mac/agentbus_service.py`); `wait` still
blocks. ADR 0026 is still Proposed.

---

## 5. Fleet and execution (diagram 04)

| Claim | Source |
|---|---|
| macOS nodes are host installs under launchd | ADR 0015 **Accepted** |
| Linux: native steward + containerized execution | ADR 0012 Accepted, implementation deferred; ADR 0015 narrows containers to Linux |
| Five coding-agent routes, opencode first | `AGENT_PRIORITY` in `src/mac/coding_agent.py` |
| Docker Engine/Moby is the only container runtime | ADR 0008 |
| Secrets are handles | `README.md` Core Contracts |
| Egress declared per project and per task | `mac project egress`, `mac task egress` in `docs/reference/cli.md` |

---

## 6. Measurement (diagram 05)

Token-routing figures are **not re-measured**. They remain the seven days
to 2026-08-19 from ADR 0017:

| Claim | Source |
|---|---|
| 28,352 `llm.route` events; 481.8M input / 5.05M output; 64% streaming; 29.5% null `input_tokens`; 0 cached | ADR 0017, "Measured over the seven days to 2026-08-19" |
| Cost is priced at read time | ADR 0017 `estimate_route_cost()` |

Ledger census **is** re-measured. `mac task stats --json` against the live
hub at capture 2026-08-25T00:08Z (counts only; no agent names):

| state | count |
|---|---|
| blocked | 378 |
| waiting | 29 |
| open | 94 |
| running | 1 |
| failed | 2,160 |
| cancelled | 3,565 |
| completed | 728 |
| reviewing | 18 |
| needs_input | 22 |
| needs_review | 1 |
| stopped | 4 |

The 165-of-355 permanently-dead blocked finding is still ADR 0018's
2026-08-19 measurement. That dead-dependency query was **not** re-run.
Blocked has grown from 355 to 378.

ADR 0016 is no longer Proposed; the three open decisions on the slide are
0029, 0017, and 0018. Sixteen ADRs remain Proposed overall (§7).

---

## 7. Statuses, stated precisely

| ADR | Status in the file |
|---|---|
| 0001 vendored Hermes | **Superseded** (vendoring premise ended 2026-08-17) |
| 0010 Fleet IDE cut-over | **Superseded** |
| 0012 native steward + containerized execution | **Accepted; implementation deferred pending fleet measurement** |
| 0015 macOS nodes are host installs | **Accepted** |
| 0016 agents decide what a task needs | **Accepted** (accepted 2026-08-20); hub still drives the review workflow |
| 0017 token spend metered at the router | **Proposed** |
| 0018 task view is a graph | **Proposed** |
| 0025 hub UI is the observability console | **Accepted** |
| 0029 coding-route search path is a fleet contract | **Proposed** |
| 0030 LangChain extracts before Qdrant | **Proposed** |

Also **Proposed**: 0002, 0003, 0005, 0006, 0020, 0021, 0022, 0023, 0024,
0026, 0027, 0028. Sixteen Proposed in total.

---

## 8. Where the README (and generated help) is stale

Noted because the deck contradicts them deliberately:

- `README.md` still documents src/mac/_hermes as a vendored Hermes Agent
  0.15.1 snapshot supplying "the agent loop, gateways, tools, plugins, and
  skills". That directory does not exist at this commit. The vendored tree
  was removed in `3ebde2dd`. The path is left unbackticked here because a
  backticked repository path asserts that it exists
  (`tests/test_guide_docs_are_true.py`).
- The testing and linting sections of `README.md` still claim that path is
  excluded from coverage and from Ruff. Nothing in `pyproject.toml` or the
  `Makefile` references `_hermes`.
- Current (non-archive) docs that still name the missing tree as if it were
  present: `docs/audit.md`, `docs/home-consolidation.md`,
  `docs/hermes-integration.md`, `docs/oneshot-isolation-gate-verification.md`,
  `docs/reference/staged-module-integration-audit.md`. `docs/hermes-vendor-fate.md`
  correctly says **removed**.
- Generated `docs/reference/cli.md` `mac --help` text claims "0
  administrative commands live under `mac admin`" while listing 54 groups
  immediately below.

**Not stale:** `mac-hermes = "mac.hermes_adapter:main"` in `pyproject.toml`
and `src/mac/hermes_adapter.py` exist. The adapter is clean-room MAC code.
`docs/hermes-retirement-premises.md` still records that both premises for
retiring Hermes "neither holds as stated".

The deck does not describe the vendored snapshot as present, and does not
claim Hermes has been retired as an interaction boundary.

---

## 9. What landed since v1.1.0 (for the GitHub notes, not the slides)

`git log --oneline v1.1.0..e8040fec` is 129 commits. The capabilities that
are newly *true of this tree*, rather than merely newly committed:

- Hub judgement replaces the semantic reviewer (`#644`).
- OpenClaw identity and migrated cron jobs survive deploy (`#657`).
- Operator-managed hub database is preserved (`#658`).
- Docs-graph is a standing release gate (`#649`).
- Native merge queue repair after a stuck GitHub queue (`#639`).
- ADRs 0026–0031 exist as files; 0029 and 0030 are Proposed; 0031 is Accepted.
- `STOPPED` is a task state; `judgement` is an admin group; CLI on coding-agent
  shell paths (`#652`); sandboxes pin reviewed runtime digests (`#651`).

Proposed ADRs are not listed as shipped capabilities.

---

## 10. Provenance of this directory

| | |
|---|---|
| Commit described | `e8040fec1cda998fa0ef19eb3cec9a516537b247` |
| Capture | 2026-08-25T00:08:16Z |
| Directory | `docs/presentation/20260825T000816Z-e8040fec/` |
| Previous sibling | `docs/presentation/20260820T011224Z-8b424c20/` |

---

## 11. Per-file decisions (inventory)

Every row of `docs/reference/documentation-inventory.md` at `e8040fec`.
Pinned decks under `docs/presentation/` are allowlisted and are not in this table.

| Category | Source | Decision |
|---|---|---|
| supplemental reference | `activation-probe/calibration-spec.md` | not-changed against tree at this SHA |
| supplemental reference | `activation-probe/classifier-spec.md` | not-changed against tree at this SHA |
| supplemental reference | `activation-probe/integration-guide.md` | not-changed against tree at this SHA |
| supplemental reference | `activation-probe/prototype-report.md` | not-changed against tree at this SHA |
| supplemental reference | `activation-probe/runtime-selection.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0001-unify-hermes-runtime-into-mac.md` | not-changed; status in §7 |
| architecture decision | `adr/0002-memory-store-at-scale.md` | not-changed; status in §7 |
| architecture decision | `adr/0003-tokenhub-core-into-mac.md` | not-changed; status in §7 |
| architecture decision | `adr/0004-task-ledger-vs-hermes-kanban.md` | not-changed; status in §7 |
| architecture decision | `adr/0005-elastic-executor-tier-vs-static-fleet.md` | not-changed; status in §7 |
| architecture decision | `adr/0006-acp-support.md` | not-changed; status in §7 |
| architecture decision | `adr/0007-hermes-boundary-mood-nap-soul-memory.md` | not-changed; status in §7 |
| architecture decision | `adr/0008-openshell-docker-engine-runtime.md` | not-changed; status in §7 |
| architecture decision | `adr/0009-minimal-base-on-demand-layered-provisioning.md` | not-changed; status in §7 |
| architecture decision | `adr/0010-fleet-ide-cutover-parity-matrix.md` | not-changed; status in §7 |
| architecture decision | `adr/0011-hub-review-verification-scope.md` | not-changed; status in §7 |
| architecture decision | `adr/0012-hybrid-native-steward-containerized-execution.md` | not-changed; status in §7 |
| architecture decision | `adr/0013-authoritative-hub-allocator.md` | not-changed; status in §7 |
| architecture decision | `adr/0014-visibility-is-not-a-dispatch-gate.md` | not-changed; status in §7 |
| architecture decision | `adr/0015-macos-nodes-are-host-installs.md` | not-changed; status in §7 |
| architecture decision | `adr/0016-agent-initiated-review.md` | not-changed; status in §7 |
| architecture decision | `adr/0017-token-spend-is-metered-at-the-router.md` | not-changed; status in §7 |
| architecture decision | `adr/0018-task-graph-progressive-disclosure.md` | not-changed; status in §7 |
| architecture decision | `adr/0019-privilege-is-an-acl-on-a-resource-tree.md` | not-changed; status in §7 |
| architecture decision | `adr/0020-a-running-task-is-not-editable.md` | not-changed; status in §7 |
| architecture decision | `adr/0021-schema-changes-need-versioned-migrations.md` | not-changed; status in §7 |
| architecture decision | `adr/0022-a-gate-returns-a-named-decision-not-a-boolean.md` | not-changed; status in §7 |
| architecture decision | `adr/0023-one-skill-source-many-harness-plugins.md` | not-changed; status in §7 |
| architecture decision | `adr/0024-the-dashboard-streams-the-bus-not-just-its-counts.md` | not-changed; status in §7 |
| architecture decision | `adr/0025-the-hub-ui-is-the-observability-console.md` | not-changed; status in §7 |
| architecture decision | `adr/0026-first-class-operations-emit-bus-events.md` | not-changed; status in §7 |
| architecture decision | `adr/0027-upgrades-are-versioned-and-fail-closed.md` | not-changed; status in §7 |
| architecture decision | `adr/0028-installation-is-a-package-not-a-push.md` | not-changed; status in §7 |
| architecture decision | `adr/0029-the-route-search-path-is-a-fleet-contract.md` | not-changed; status in §7 |
| architecture decision | `adr/0030-langchain-extracts-before-qdrant.md` | not-changed; status in §7 |
| supplemental reference | `agent-lifecycle-proof.md` | not-changed against tree at this SHA |
| historical archive | `archive/field-notes/assessment-2026-08-02.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/assessment-task-1b6783.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/assessment-task-21e771-worker3-tailscale-blocker.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/assessment-task-7023d6.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/assessment-task-83f38e.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/assessment-task-8bf378.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/assessment-task-97627e.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/assessment-task-a33145.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/assessment-task-a608f4.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/assessment-task-b07fbf.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/assessment-task-de3502.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/assessment-task-f6a813.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/assessment-task-f9cd72.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/closeout-dreamrepair-3dc2cf-openclaw-fleet-rollout.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/closeout-dreamrepair-5404b15-skill.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/closeout-dreamrepair-965c6e89-openclaw-fleet-rollout.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/closeout-review-finalize-verify-prerequisite.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/closeout-task-9c83aa5b-skill.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/contract-verify-environment-failure-finding.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/crash-incident-finding.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/disposition-dreamrepair-c8dd8037-skill.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/disposition-dreamrepair-ecd3548-skill.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/disposition-hgx-session-c0b2f9fd4e0b.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/disposition-hgx-session-c902fab4d55f.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/disposition-task-394db89d-slack.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/disposition-task-46eb6c-skill-env-prereqs.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/disposition-task-9c83aa5b-skill.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/disposition-task-c3a30819-skill.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/disposition-task-cc1dedb0-slack.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/dream-finding-3dc2cf.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/dream-finding-58afe2-openclaw-entrypoint-ready-token.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/dream-finding-6d1b5b.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/dream-finding-805aed7.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/dream-finding-965c6e89.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/dream-stalled-finalizer-recovery-finding.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/dream-triage-828e1ef4.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/findings-crash-1fc349e1-startup-selftest-attestation-gap.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/findings-crash-591645a3-startup-selftest-timeout.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/findings-crash-mac-agent-service-62021be0.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/findings-crash-service-unknown-crash-path.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/fleet-ide-workbench-plan.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/forensics-task-643b33ee1c7b4a4ab7a81bf8d5af34a4.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/forensics-task-a32a35e90ab0434e8c7766057b268bc6.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/haskell_migration.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-dream-skill-generic-area-bucket.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-dream-skill-tool_or_skill_name-actionability.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-dream-tests-generic-area-bucket.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-dreamrepair-394db89d-slack.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-dreamrepair-477446f5-slack.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-dreamrepair-4c4429b-scripts-openclaw.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-dreamrepair-5404b15-skill.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-dreamrepair-71b00e8-slack.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-dreamrepair-c8dd8037-skill.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-dreamrepair-cc1dedb0-slack.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-dreamrepair-d94ad78-skill.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-dreamrepair-da0ac0f3-slack.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-dreamrepair-ffbc63f8-skill.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-predispatch-conflict-5a43ad.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-review-finalize-verify-prerequisite.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-task-2284f3-env-prerequisite-false-positive.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-task-b6ddd8-skill-env-prereqs.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-task-ed7b0b-new-file-staging-finalizer.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/investigation-task-f869c0-new-file-staging-finalizer.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/job-per-task-roles-spec-review.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/job-per-task-roles-spec.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/k8s-native-rewrite-plan.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/linear-bridge-spec-review.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/linear-bridge-spec.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/mac-task-bd-parity-audit.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/metadata-sync-assessment.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/prereq-task-029665.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/prereq-task-403ed263.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/prereq-task-e94f546c.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/prereq-task-fd2f34.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/provenance-dreamrepair-77fc3e59-slack.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/quickstart-gap-analysis.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/replace-hgx-session-c902fab4d55f.md` | not current behaviour (archive index) |
| historical archive | `archive/field-notes/scaling-plan.md` | not current behaviour (archive index) |
| historical archive | `archive/index.md` | not current behaviour (archive index) |
| supplemental reference | `audit.md` | CHANGED finding: names missing `_hermes` tree (§8) |
| supplemental reference | `authority-boundary.md` | not-changed against tree at this SHA |
| book | `book/01-system.md` | not-changed; `make docs-check` executes examples |
| book | `book/02-local-start.md` | not-changed; `make docs-check` executes examples |
| book | `book/03-projects-and-tasks.md` | not-changed; `make docs-check` executes examples |
| book | `book/04-machines-and-agents.md` | not-changed; `make docs-check` executes examples |
| book | `book/05-evidence-review-completion.md` | not-changed; `make docs-check` executes examples |
| book | `book/06-hermes-and-ide.md` | not-changed; `make docs-check` executes examples |
| book | `book/07-repository-contracts.md` | not-changed; `make docs-check` executes examples |
| book | `book/08-plans-and-dags.md` | not-changed; `make docs-check` executes examples |
| book | `book/09-fleet-onboarding.md` | not-changed; `make docs-check` executes examples |
| book | `book/10-identity-and-secrets.md` | not-changed; `make docs-check` executes examples |
| book | `book/11-publication-and-refs.md` | not-changed; `make docs-check` executes examples |
| book | `book/12-operations.md` | not-changed; `make docs-check` executes examples |
| book | `book/13-deployment-topologies.md` | not-changed; `make docs-check` executes examples |
| book | `book/14-images-and-cutover.md` | not-changed; `make docs-check` executes examples |
| book | `book/15-sandboxed-runtimes.md` | not-changed; `make docs-check` executes examples |
| book | `book/16-apis-and-integrations.md` | not-changed; `make docs-check` executes examples |
| book | `book/17-learning-evals-scaling.md` | not-changed; `make docs-check` executes examples |
| book | `book/18-capstone.md` | not-changed; `make docs-check` executes examples |
| runbook | `break-glass-host-recovery.md` | not-changed against tree at this SHA |
| supplemental reference | `c26-certifier-phase-profile-example.md` | not-changed against tree at this SHA |
| supplemental reference | `client-bootstrap-contract.md` | not-changed against tree at this SHA |
| supplemental reference | `coding-cli-credentials.md` | not-changed against tree at this SHA |
| supplemental reference | `coding-route-ladder.md` | not-changed against tree at this SHA |
| supplemental reference | `crash-diagnosis-and-repair.md` | not-changed against tree at this SHA |
| supplemental reference | `dashboard-connection.md` | not-changed against tree at this SHA |
| supplemental reference | `deploy-prerequisite-vs-phase1-audit.md` | not-changed against tree at this SHA |
| supplemental reference | `dispatch-priority-bias-audit.md` | not-changed against tree at this SHA |
| supplemental reference | `dream-repair-slack-lineage.md` | not-changed against tree at this SHA |
| supplemental reference | `dreaming-rewrite.md` | not-changed against tree at this SHA |
| supplemental reference | `env-config-reference.md` | not-changed against tree at this SHA |
| runbook | `fleet-cutover-transaction-protocol.md` | not-changed against tree at this SHA |
| supplemental reference | `fleet-directives.md` | not-changed against tree at this SHA |
| runbook | `fleet-node-onboarding-checklist.md` | not-changed against tree at this SHA |
| supplemental reference | `fleet-operational-learning.md` | not-changed against tree at this SHA |
| supplemental reference | `fleet-registry-schema.md` | not-changed against tree at this SHA |
| supplemental reference | `getting-started.md` | not-changed against tree at this SHA |
| supplemental reference | `guide/01-architecture.md` | not-changed; `tests/test_guide_docs_are_true.py` |
| supplemental reference | `guide/02-getting-started.md` | not-changed; `tests/test_guide_docs_are_true.py` |
| supplemental reference | `guide/03-advanced.md` | not-changed; `tests/test_guide_docs_are_true.py` |
| supplemental reference | `guide/04-ui.md` | not-changed; `tests/test_guide_docs_are_true.py` |
| supplemental reference | `guide/05-developer-guide.md` | not-changed; `tests/test_guide_docs_are_true.py` |
| supplemental reference | `guide/README.md` | not-changed; `tests/test_guide_docs_are_true.py` |
| supplemental reference | `hermes-boundary.md` | not-changed against tree at this SHA |
| supplemental reference | `hermes-integration.md` | CHANGED finding: names missing `_hermes` tree (§8) |
| supplemental reference | `hermes-retirement-premises.md` | not-changed against tree at this SHA |
| supplemental reference | `hermes-vendor-fate.md` | not-changed; correctly records removal |
| supplemental reference | `hgx-elastic-capacity.md` | not-changed against tree at this SHA |
| supplemental reference | `home-consolidation.md` | CHANGED finding: names missing `_hermes` tree (§8) |
| runbook | `hub-availability.md` | not-changed against tree at this SHA |
| supplemental reference | `hub-host-saturation-remediation.md` | not-changed against tree at this SHA |
| supplemental reference | `human-interface-selector.md` | not-changed against tree at this SHA |
| supplemental reference | `image-publication-and-qualification.md` | not-changed against tree at this SHA |
| supplemental reference | `in-flight-agent-messages.md` | not-changed against tree at this SHA |
| landing page | `index.md` | not-changed against tree at this SHA |
| supplemental reference | `integration-authority-contract.md` | not-changed against tree at this SHA |
| supplemental reference | `memory-tier-schema.md` | not-changed against tree at this SHA |
| supplemental reference | `memory-tier-verification.md` | not-changed against tree at this SHA |
| supplemental reference | `notifier-configuration-guide.md` | not-changed against tree at this SHA |
| supplemental reference | `oneshot-isolation-gate-verification.md` | CHANGED finding: names missing `_hermes` tree (§8) |
| supplemental reference | `openclaw-identities.md` | not-changed against tree at this SHA |
| supplemental reference | `openshell-nemo-relay-e2e.md` | not-changed against tree at this SHA |
| supplemental reference | `openshell-nemo-relay-integration.md` | not-changed against tree at this SHA |
| supplemental reference | `openshell-sandbox.md` | not-changed against tree at this SHA |
| runbook | `production-deployment.md` | not-changed against tree at this SHA |
| generated reference | `reference/cli.md` | not-changed; CI `--check` on this SHA |
| generated reference | `reference/documentation-inventory.md` | not-changed; CI `--check` on this SHA |
| generated reference | `reference/openapi.md` | not-changed; CI `--check` on this SHA |
| generated reference | `reference/staged-module-integration-audit.md` | not-changed; CI `--check` on this SHA |
| supplemental reference | `repository-cicd-monitor.md` | not-changed against tree at this SHA |
| supplemental reference | `repository-ref-hygiene.md` | not-changed against tree at this SHA |
| supplemental reference | `repository-runtime-contract.md` | not-changed against tree at this SHA |
| supplemental reference | `review-strategy-experiments.md` | not-changed against tree at this SHA |
| supplemental reference | `review-tick-stall-diagnosis.md` | not-changed against tree at this SHA |
| supplemental reference | `scientific-optimizer.md` | not-changed against tree at this SHA |
| supplemental reference | `secrets-management-guide.md` | not-changed against tree at this SHA |
| supplemental reference | `security/openshell-0.0.72-compatibility-review.mdx` | not-changed against tree at this SHA |
| runbook | `soul-preservation-runbook.md` | not-changed against tree at this SHA |
| supplemental reference | `structured-task-bodies.md` | not-changed against tree at this SHA |
| historical archive | `superpowers/plans/2026-05-31-autonomous-project-routing-review-fix-loop.md` | not current behaviour (archive index) |
| historical archive | `superpowers/specs/2026-05-31-autonomous-review-fix-loop-design.md` | not current behaviour (archive index) |
| historical archive | `superpowers/specs/2026-06-04-k8s-bootstrap-fleet-registration-design.md` | not current behaviour (archive index) |
| runbook | `synchronized-fleet-cutover.md` | not-changed against tree at this SHA |
| supplemental reference | `task-dependency-semantics.md` | not-changed against tree at this SHA |
| supplemental reference | `task-throughput-observability.md` | not-changed against tree at this SHA |
| supplemental reference | `testing-strategy.md` | not-changed against tree at this SHA |

Root `README.md` is outside `docs/` and is the stale `_hermes` finding in §8.
