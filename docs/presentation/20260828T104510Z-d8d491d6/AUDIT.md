# Audit — every claim in this deck, traced to source

Audited at commit `d8d491d6` on 2026-08-28T10:45:10Z, by reading the tree
directly. Generated references (`docs/reference/cli.md`,
`docs/reference/openapi.md`) are treated as authoritative for surface counts
because CI fails when they drift from the live parser and OpenAPI schema.

**Where the README and the code disagree, this audit follows the code**, and
says so.

The previous capabilities deck (`e8040fec`, 2026-08-25, v1.2.0) is not
updated. This directory is a sibling, pinned to this SHA.

---

## 0. Current-doc pass

`docs/reference/documentation-inventory.md` at this commit lists every
Markdown file under `docs/` except pinned decks. Each row got a
changed / not-changed decision against the tree. The summary:

| Class in the inventory | Decision | Source anchor |
|---|---|---|
| generated reference (`cli.md`, `openapi.md`, env registry, this inventory) | **changed** (regenerated) | `Makefile` `docs-build` runs `scripts/generate-docs-reference.py --check`; Documentation CI on `d8d491d6` succeeded |
| architecture decision `docs/adr/` (32 files; 0001–0030, 0032, 0033; no 0031) | statuses in §7; files that moved since `e8040fec` marked **changed** in §11 | status line of each ADR |
| book chapters `book/01`–`book/18` | four chapters **changed**; rest not-changed | `mkdocs.yml` `nav:`; `make docs-check` executes every shell example |
| guide `docs/guide/` | `02` and `05` **changed**; rest not-changed | `tests/test_guide_docs_are_true.py` |
| historical archive | **not current behaviour** | `docs/archive/index.md` label; inventory category `historical archive` |
| remaining current supplemental / runbook docs | mixed; see §11 | tree vs each file |

The full per-file table is §11. No current inventory row was left unchecked.

---

## 1. Counts

| Claim | How it was obtained |
|---|---|
| 207,258 lines of Python under `src/` | `find src -name "*.py" \| xargs wc -l` at `d8d491d6` |
| 219 modules in `src/mac` | `ls src/mac/*.py \| wc -l` |
| 498 test files | `ls tests/*.py \| wc -l` (top-level; 572 including `tests/cli` and `tests/ui`) |
| 430 HTTP routes | `grep -cE "^\\| \\`(GET\\|POST\\|PUT\\|PATCH\\|DELETE)" docs/reference/openapi.md` |
| 125 CLI verbs | Parsed from `docs/reference/cli.md`: task 45, project 9, agent 17, admin 54 |
| 18 book chapters | `nav:` in `mkdocs.yml`, `book/01`–`book/18` |
| 32 ADRs | 32 files under `docs/adr/`; 16 are Proposed. ADR 0031 was removed with CodeGraph (`b02e2ef5`). ADR 0032 and 0033 are new since v1.2.0 |
| 5 coding-agent routes | `src/mac/coding_agent.py` `AGENT_PRIORITY`: `opencode`, `pi`, `claude`, `codex`, `cursor` |
| 50 commits since `v1.2.0` | `git rev-list --count v1.2.0..d8d491d6` |

The generated `mac --help` text still says "0 administrative commands live under
`mac admin`". The same generated `docs/reference/cli.md` then lists 54 admin
groups, including `judgement`. The deck uses 54, not 0. Recorded as a
generator defect in §8.

---

## 2. The object model (diagram 01)

Unchanged in substance from the `e8040fec` audit. Verb counts per object are
still 9 / 45 / 17 / 54. HTTP routes grew from 414 to 430. Twelve task states
remain. `judgement` remains an admin group. `/ui` is still read-only
observability (ADR 0025 Accepted).

| Claim | Source |
|---|---|
| The CLI is organised around project / task / agent / admin | `docs/reference/cli.md`, `mac --help` |
| Project dispatch pause is separate from per-task staging | `README.md` Core Contracts |
| Recovery verbs | `mac task --help`: `recover-stranded`, `recover-finalizer`, `recover-stalled-finalizer` |
| Break-glass is grantable/listable/revocable | `mac task --help` |
| Visibility is not a dispatch gate | `docs/adr/0014-visibility-is-not-a-dispatch-gate.md` |
| Generated CLI reference is checked | `scripts/generate-docs-reference.py`; `Makefile` `docs-build --check` |
| MCP server is a client, not a second implementation | commit `efb428e9` |
| Python client is contract-checked against the hub | `tests/test_dispatch_route_contract.py` |
| ACP and A2A are implemented specifications | `/.well-known/acp`, `/.well-known/agent-card.json`, `/a2a` in `docs/reference/openapi.md` |
| `/ui` is read-only observability | `docs/adr/0025-the-hub-ui-is-the-observability-console.md` |
| 125 verbs, 430 routes | §1 |

---

## 3. Task lifecycle (diagram 02)

Twelve states still, including `STOPPED`. `src/mac/models.py` `TaskState` is
unchanged in membership. Native merge queue and classified failures remain.
Canonical reconcile is new: a recorded look at canonical HEAD is required
before treating a `repo_change` as still required (`src/mac/canonical_reconcile.py`,
`#672`). That look is evidence, not an auto-close: `already_satisfied` /
`needs_restatement` do not by themselves complete or cancel a task on this
commit.

Hub still drives RUNNING → NEEDS_REVIEW → REVIEWING (`advance_default_review_workflow`).
ADR 0016 remains Accepted as a decision and is not yet the runtime.

---

## 4. Coordination (diagram 03)

Unchanged in substance. AgentBus is still a broadcast bus
(`src/mac/agentbus_control.py`). Lifecycle verbs remain stand_down, abort,
pause, resume, status. ADR 0026 is still Proposed. ADR 0023 is now
**Accepted** (Agent Plugins installer, `#662`). ADR 0032 (harness hooks, not
tmux) is **Proposed**. ADR 0033 (local continuation under hub supervision)
is **Accepted** (`#666`).

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
| `glab` for GitLab remotes, `gh` for GitHub when probing open review | `src/mac/cli.py` / repository hygiene, `#676` |

---

## 6. Measurement (diagram 05)

Token-routing figures are **not re-measured**. They remain the seven days
to 2026-08-19 from ADR 0017:

| Claim | Source |
|---|---|
| 28,352 `llm.route` events; 481.8M input / 5.05M output; 64% streaming; 29.5% null `input_tokens`; 0 cached | ADR 0017, "Measured over the seven days to 2026-08-19" |
| Cost is priced at read time | ADR 0017 `estimate_route_cost()` |

Ledger census **is** re-measured. `mac task stats --json` against the live
hub at capture 2026-08-28T10:45Z (counts only; no agent names):

| state | count |
|---|---|
| blocked | 380 |
| waiting | 29 |
| open | 124 |
| running | 0 |
| failed | 2,165 |
| cancelled | 3,566 |
| completed | 740 |
| reviewing | 17 |
| needs_input | 22 |
| needs_review | 1 |
| stopped | 5 |

The 165-of-355 permanently-dead blocked finding is still ADR 0018's
2026-08-19 measurement. That dead-dependency query was **not** re-run.
Blocked has grown from 378 (v1.2.0 capture) to 380.

ADR 0016 remains Accepted; the three open decisions on the slide are still
0029, 0017, and 0018. Sixteen ADRs remain Proposed overall (§7).

---

## 7. Statuses, stated precisely

| ADR | Status in the file |
|---|---|
| 0001 vendored Hermes | **Superseded** (vendoring premise ended 2026-08-17) |
| 0010 Fleet IDE cut-over | **Superseded** |
| 0012 native steward + containerized execution | **Accepted; implementation deferred pending fleet measurement** |
| 0015 macOS nodes are host installs | **Accepted** |
| 0016 agents decide what a task needs | **Accepted**; hub still drives the review workflow |
| 0017 token spend metered at the router | **Proposed** |
| 0018 task view is a graph | **Proposed** |
| 0023 one skill source, thin plugins | **Accepted** (amended 2026-08-25; shipped `#662`) |
| 0025 hub UI is the observability console | **Accepted** |
| 0029 coding-route search path is a fleet contract | **Proposed** |
| 0030 LangChain extracts before Qdrant | **Proposed** |
| 0031 CodeGraph is a hint | **removed** with CodeGraph (`b02e2ef5`) |
| 0032 CLI sessions use harness hooks, not tmux | **Proposed** |
| 0033 local continuation under hub supervision | **Accepted** (`#666`) |

Also **Proposed**: 0002, 0003, 0005, 0006, 0020, 0021, 0022, 0024,
0026, 0027, 0028. Sixteen Proposed in total (0023 left the set; 0032 entered it).

---

## 8. Where the README (and generated help) is stale

Noted because the deck contradicts them deliberately:

- `README.md` still documents src/mac/_hermes as a vendored Hermes Agent
  snapshot supplying "the agent loop, gateways, tools, plugins, and
  skills". That directory does not exist at this commit. The vendored tree
  was removed in `3ebde2dd`. The path is left unbackticked here because a
  backticked repository path asserts that it exists
  (`tests/test_guide_docs_are_true.py`).
- The testing and linting sections of `README.md` still claim that path is
  excluded from coverage and from Ruff. Nothing in `pyproject.toml` or the
  `Makefile` references `_hermes`.
- Current (non-archive) docs that still name the missing tree as if it were
  present include `docs/audit.md`, `docs/home-consolidation.md`,
  `docs/hermes-integration.md`, `docs/oneshot-isolation-gate-verification.md`.
  `docs/hermes-vendor-fate.md` correctly says **removed**.
- Generated `docs/reference/cli.md` `mac --help` text claims "0
  administrative commands live under `mac admin`" while listing 54 groups
  immediately below.

**Not stale:** `mac-hermes = "mac.hermes_adapter:main"` in `pyproject.toml`
and `src/mac/hermes_adapter.py` exist. The adapter is clean-room MAC code.

The deck does not describe the vendored snapshot as present, and does not
claim Hermes has been retired as an interaction boundary.

**Release-gate notes, not slide claims:**

- CI on `d8d491d6`: Documentation, mainline, compatibility, Live PostgreSQL
  contract, dead-code, IDE, and observability-console jobs succeeded.
  The `portfolio` job failed because it tries to push a refreshed test-impact
  map directly to `refs/heads/main` and branch protection rejects that
  (ledger `task_1746351d`). That is not a product-test failure.
- `make lint` on this Darwin host reports ruff format drift in 8 already-committed
  files (`check_rc=0`, `format_rc=1`). CI mainline still passed.
- `make test` under pytest-xdist on this Darwin host failed 9 `process_e2e`
  cases with subprocess timeouts; the same 11 cases passed in 29s with
  `-n 0` (ledger `task_e00b069b`).

---

## 9. What landed since v1.2.0 (for the GitHub notes, not the slides)

`git log --oneline v1.2.0..d8d491d6` is 50 commits. Capabilities that are
newly *true of this tree*:

- A recorded canonical-HEAD reconcile look is required before treating a
  repository change as still required (`#672`). Auto-close from that look is
  not on this commit.
- Leftover work-package task triggers that blocked every claim are gone (`#671`).
- Test-impact map regeneration is an explicit dependency of its consumers (`#673`).
- Darwin `make install` lockfile includes optional rolldown platform bindings;
  GitLab remotes use `glab` when probing open review (`#676`).
- Fail-closed PostgreSQL migration authority, backup-gated legacy schema
  pruning, and `psql`/`pg_restore` on PATH during live backup verify
  (`#674`, `#675`, and related).
- ADR 0023 shipped: Agent Plugins installer and hub-only stall nudge (`#662`).
- ADR 0033 shipped: bounded local continuation under hub supervision (`#666`).
- Hub-mediated fleet self-upgrades; OpenClaw home routing on Darwin; nightly
  local-news limited to one real broadcast per day.
- CodeGraph purged (`#664`); tests that do not protect public behaviour pruned.
- Task throughput made responsive on a large live ledger (`#667` / related).

Proposed ADRs are not listed as shipped capabilities.

---

## 10. Provenance of this directory

| | |
|---|---|
| Commit described | `d8d491d68902087c27714c92e6edbaea2c590ed1` |
| Capture | 2026-08-28T10:45:10Z |
| Directory | `docs/presentation/20260828T104510Z-d8d491d6/` |
| Previous sibling | `docs/presentation/20260825T000816Z-e8040fec/` |
| Tag this release intends | `v1.3.0` |

---

## 11. Per-file decisions (inventory)

Every row of `docs/reference/documentation-inventory.md` at `d8d491d6`.
Pinned decks under `docs/presentation/` are allowlisted and are not in this table.

| Category | Source | Decision |
|---|---|---|
| supplemental reference | `activation-probe/calibration-spec.md` | not-changed against tree at this SHA |
| supplemental reference | `activation-probe/classifier-spec.md` | not-changed against tree at this SHA |
| supplemental reference | `activation-probe/integration-guide.md` | not-changed against tree at this SHA |
| supplemental reference | `activation-probe/prototype-report.md` | not-changed against tree at this SHA |
| supplemental reference | `activation-probe/runtime-selection.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0001-unify-hermes-runtime-into-mac.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0002-memory-store-at-scale.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0003-tokenhub-core-into-mac.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0004-task-ledger-vs-hermes-kanban.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0005-elastic-executor-tier-vs-static-fleet.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0006-acp-support.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0007-hermes-boundary-mood-nap-soul-memory.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0008-openshell-docker-engine-runtime.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| architecture decision | `adr/0009-minimal-base-on-demand-layered-provisioning.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| architecture decision | `adr/0010-fleet-ide-cutover-parity-matrix.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0011-hub-review-verification-scope.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| architecture decision | `adr/0012-hybrid-native-steward-containerized-execution.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0013-authoritative-hub-allocator.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0014-visibility-is-not-a-dispatch-gate.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0015-macos-nodes-are-host-installs.md` | **changed** vs e8040fec — Separate Docker-for-system-services from OpenShell sandboxes on macOS |
| architecture decision | `adr/0016-agent-initiated-review.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| architecture decision | `adr/0017-token-spend-is-metered-at-the-router.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0018-task-graph-progressive-disclosure.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0019-privilege-is-an-acl-on-a-resource-tree.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0020-a-running-task-is-not-editable.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0021-schema-changes-need-versioned-migrations.md` | **changed** vs e8040fec — Add fail-closed PostgreSQL migration authority |
| architecture decision | `adr/0022-a-gate-returns-a-named-decision-not-a-boolean.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0023-one-skill-source-many-harness-plugins.md` | **changed** vs e8040fec — Implement ADR 0023: Agent Plugins installer and hub-only stall nudge. (#662) |
| architecture decision | `adr/0024-the-dashboard-streams-the-bus-not-just-its-counts.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0025-the-hub-ui-is-the-observability-console.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0026-first-class-operations-emit-bus-events.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0027-upgrades-are-versioned-and-fail-closed.md` | **changed** vs e8040fec — Add backup-gated legacy schema pruning |
| architecture decision | `adr/0028-installation-is-a-package-not-a-push.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| architecture decision | `adr/0029-the-route-search-path-is-a-fleet-contract.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0030-langchain-extracts-before-qdrant.md` | not-changed against tree at this SHA |
| architecture decision | `adr/0032-cli-session-hooks-not-tmux.md` | **changed** vs e8040fec — Record that CLI sessions use harness hooks, not tmux, for AgentBus I/O. (#659) |
| architecture decision | `adr/0033-local-continuation-hub-supervision.md` | **changed** vs e8040fec — Add bounded local continuation under hub supervision (#666) |
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
| supplemental reference | `audit.md` | not-changed against tree at this SHA |
| supplemental reference | `authority-boundary.md` | not-changed against tree at this SHA |
| book | `book/01-system.md` | not-changed against tree at this SHA |
| book | `book/02-local-start.md` | not-changed against tree at this SHA |
| book | `book/03-projects-and-tasks.md` | not-changed against tree at this SHA |
| book | `book/04-machines-and-agents.md` | not-changed against tree at this SHA |
| book | `book/05-evidence-review-completion.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| book | `book/06-hermes-and-ide.md` | not-changed against tree at this SHA |
| book | `book/07-repository-contracts.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| book | `book/08-plans-and-dags.md` | not-changed against tree at this SHA |
| book | `book/09-fleet-onboarding.md` | not-changed against tree at this SHA |
| book | `book/10-identity-and-secrets.md` | not-changed against tree at this SHA |
| book | `book/11-publication-and-refs.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| book | `book/12-operations.md` | not-changed against tree at this SHA |
| book | `book/13-deployment-topologies.md` | not-changed against tree at this SHA |
| book | `book/14-images-and-cutover.md` | not-changed against tree at this SHA |
| book | `book/15-sandboxed-runtimes.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| book | `book/16-apis-and-integrations.md` | not-changed against tree at this SHA |
| book | `book/17-learning-evals-scaling.md` | not-changed against tree at this SHA |
| book | `book/18-capstone.md` | not-changed against tree at this SHA |
| runbook | `break-glass-host-recovery.md` | not-changed against tree at this SHA |
| supplemental reference | `c26-certifier-phase-profile-example.md` | not-changed against tree at this SHA |
| supplemental reference | `client-bootstrap-contract.md` | not-changed against tree at this SHA |
| supplemental reference | `coding-cli-credentials.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| supplemental reference | `coding-route-ladder.md` | not-changed against tree at this SHA |
| supplemental reference | `crash-diagnosis-and-repair.md` | not-changed against tree at this SHA |
| supplemental reference | `dashboard-connection.md` | not-changed against tree at this SHA |
| supplemental reference | `deploy-prerequisite-vs-phase1-audit.md` | not-changed against tree at this SHA |
| supplemental reference | `dispatch-priority-bias-audit.md` | not-changed against tree at this SHA |
| supplemental reference | `dream-repair-slack-lineage.md` | not-changed against tree at this SHA |
| supplemental reference | `dreaming-rewrite.md` | not-changed against tree at this SHA |
| supplemental reference | `env-config-reference.md` | **changed** vs e8040fec — Make impact-map regeneration an explicit dependency of its consumers (#673) |
| runbook | `fleet-cutover-transaction-protocol.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| supplemental reference | `fleet-directives.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| runbook | `fleet-node-onboarding-checklist.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| supplemental reference | `fleet-operational-learning.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| supplemental reference | `fleet-registry-schema.md` | not-changed against tree at this SHA |
| supplemental reference | `getting-started.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| supplemental reference | `guide/01-architecture.md` | not-changed against tree at this SHA |
| supplemental reference | `guide/02-getting-started.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| supplemental reference | `guide/03-advanced.md` | not-changed against tree at this SHA |
| supplemental reference | `guide/04-ui.md` | not-changed against tree at this SHA |
| supplemental reference | `guide/05-developer-guide.md` | **changed** vs e8040fec — Make impact-map regeneration an explicit dependency of its consumers (#673) |
| supplemental reference | `guide/README.md` | not-changed against tree at this SHA |
| supplemental reference | `hermes-boundary.md` | not-changed against tree at this SHA |
| supplemental reference | `hermes-integration.md` | **changed** vs e8040fec — Make lint and lint-fix a diagnose/apply pair, including format (#663) |
| supplemental reference | `hermes-retirement-premises.md` | not-changed against tree at this SHA |
| supplemental reference | `hermes-vendor-fate.md` | not-changed against tree at this SHA |
| supplemental reference | `hgx-elastic-capacity.md` | not-changed against tree at this SHA |
| supplemental reference | `home-consolidation.md` | not-changed against tree at this SHA |
| runbook | `hub-availability.md` | not-changed against tree at this SHA |
| supplemental reference | `hub-host-saturation-remediation.md` | not-changed against tree at this SHA |
| supplemental reference | `human-interface-selector.md` | not-changed against tree at this SHA |
| supplemental reference | `image-publication-and-qualification.md` | not-changed against tree at this SHA |
| supplemental reference | `in-flight-agent-messages.md` | not-changed against tree at this SHA |
| landing page | `index.md` | not-changed against tree at this SHA |
| supplemental reference | `integration-authority-contract.md` | not-changed against tree at this SHA |
| supplemental reference | `memory-tier-schema.md` | **changed** vs e8040fec — Make lint and lint-fix a diagnose/apply pair, including format (#663) |
| supplemental reference | `memory-tier-verification.md` | not-changed against tree at this SHA |
| supplemental reference | `notifier-configuration-guide.md` | **changed** vs e8040fec — Make lint and lint-fix a diagnose/apply pair, including format (#663) |
| supplemental reference | `oneshot-isolation-gate-verification.md` | not-changed against tree at this SHA |
| supplemental reference | `openclaw-identities.md` | not-changed against tree at this SHA |
| supplemental reference | `openshell-nemo-relay-e2e.md` | not-changed against tree at this SHA |
| supplemental reference | `openshell-nemo-relay-integration.md` | **changed** vs e8040fec — Make lint and lint-fix a diagnose/apply pair, including format (#663) |
| supplemental reference | `openshell-sandbox.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| runbook | `production-deployment.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| generated reference | `reference/cli.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| generated reference | `reference/documentation-inventory.md` | **changed** vs e8040fec — Index the Darwin OpenClaw routing spec in generated docs. |
| generated reference | `reference/openapi.md` | **changed** vs e8040fec — Add hub-mediated fleet self-upgrades |
| generated reference | `reference/staged-module-integration-audit.md` | not-changed against tree at this SHA |
| supplemental reference | `repository-cicd-monitor.md` | not-changed against tree at this SHA |
| supplemental reference | `repository-ref-hygiene.md` | **changed** vs e8040fec — Fix Darwin make install lockfile and use glab for GitLab remotes (#676) |
| supplemental reference | `repository-runtime-contract.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| supplemental reference | `review-strategy-experiments.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| supplemental reference | `review-tick-stall-diagnosis.md` | not-changed against tree at this SHA |
| supplemental reference | `roadmap.md` | **changed** vs e8040fec — Add CLI plugin distribution milestones |
| supplemental reference | `scientific-optimizer.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| supplemental reference | `secrets-management-guide.md` | **changed** vs e8040fec — Make lint and lint-fix a diagnose/apply pair, including format (#663) |
| supplemental reference | `security/openshell-0.0.72-compatibility-review.mdx` | not-changed against tree at this SHA |
| runbook | `soul-preservation-runbook.md` | not-changed against tree at this SHA |
| supplemental reference | `structured-task-bodies.md` | not-changed against tree at this SHA |
| historical archive | `superpowers/plans/2026-05-31-autonomous-project-routing-review-fix-loop.md` | not current behaviour (archive index) |
| historical archive | `superpowers/specs/2026-05-31-autonomous-review-fix-loop-design.md` | not current behaviour (archive index) |
| historical archive | `superpowers/specs/2026-06-04-k8s-bootstrap-fleet-registration-design.md` | not current behaviour (archive index) |
| historical archive | `superpowers/specs/2026-08-22-native-darwin-openclaw-and-slack-home-routing-design.md` | not current behaviour (archive index) |
| runbook | `synchronized-fleet-cutover.md` | not-changed against tree at this SHA |
| supplemental reference | `task-dependency-semantics.md` | not-changed against tree at this SHA |
| supplemental reference | `task-throughput-observability.md` | **changed** vs e8040fec — Purge CodeGraph from MAC (#664) |
| supplemental reference | `testing-strategy.md` | **changed** vs e8040fec — Make impact-map regeneration an explicit dependency of its consumers (#673) |

Root `README.md` is outside `docs/` and is the stale `_hermes` finding in §8.

Inventory extras changed on this range but not listed above (pinned decks or
paths the generator skips): adr/0031-codegraph-is-a-hint.md, presentation/20260825T000816Z-e8040fec/README.md, presentation/20260825T000816Z-e8040fec/images/01-object-model.svg, presentation/20260825T000816Z-e8040fec/images/03-coordination.svg, presentation/20260825T000816Z-e8040fec/images/04-fleet-execution.svg, presentation/20260825T000816Z-e8040fec/images/05-measurement.svg, presentation/README.md.
