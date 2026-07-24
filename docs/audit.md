# MAC Codebase Audit — Active Code, Duplication & Accretion

*Read-only analysis. No source, test, or configuration file was modified in producing
this document. All findings are evidenced with file paths and line references valid as of
the audit date (2026-07-24, branch `main`).*

---

## 1. Executive summary

The `mac` repository is a **trustworthy coordination layer for a fleet of AI agents** — a
hub/control-plane, a worker/executor, a Kubernetes orchestrator, a standalone LLM router,
and a set of gateways, plus a large vendored agent runtime. After walking the import graph,
the entrypoint table, the test suite, and the coverage database, the headline is:

> **The codebase is overwhelmingly *active*, not dead. Its debt is not abandoned files —
> it is architectural accretion: one 26,433-line god-class, several half-finished
> decompositions, and a test suite whose size and duplication now dominate the cost of
> every change.**

Genuine dead code is small and mostly identifiable. The larger and more expensive problems
are structural, and they are the ones worth acting on.

### Scale at a glance

| Category | Files | Lines of code | Notes |
|---|---:|---:|---|
| First-party MAC code (`src/mac/`, excl. `_hermes/`) | ~200 modules + 5 sub-packages | **~185,000** | The code this repository owns and must maintain. |
| Vendored runtime (`src/mac/_hermes/`) | — | **~443,000** | Pinned snapshot of `NousResearch/hermes-agent`; excluded from coverage. |
| Tests (`tests/`) | 445 | **~200,600** | 6,883 test functions → 8,555 collected nodes. |
| **Total tracked (excl. `.venv`/`dist`/caches)** | **~1,329** | — | The 10,826 raw `.py` count is inflated by `.venv` and `__pycache__`. |

The **first-party code and the test suite are almost the same size** (185K vs 200K LOC).
That ratio — more test code than product code — is the single most important fact for both
this audit and the companion migration study: **tests are now the dominant maintenance
surface.**

### Verdict by axis

| Axis | Finding | Severity |
|---|---|---|
| Dead files | **Near-zero.** A verification pass (see §6) reclassified the apparent "orphans" as recently-staged, tested capabilities — not rot. | Low / false-positive |
| Duplication | Moderate — naming collisions, two HTTP-route paths, ledger sprawl | Medium |
| God-class | `services.py` = one 26,433-line `ControlPlane` (719 methods) | **High** |
| Test debt | 37 `_edges` coverage-companion modules; only 25% of files parametrized | **High** |
| Vendored surface | ~443K LOC pinned; most of it dormant but correctly quarantined | Low (by design) |

> **Verification note (added after the initial pass).** Every dead-code candidate below was
> checked against the full reference graph *and git history* before any action. The result
> overturned most of the dead-code hypothesis: the "orphan" modules are recent, deliberate,
> tested-first additions (one landed in the current HEAD commit), and the "dead beads plumbing"
> is a live mechanism wearing a legacy name. **The only change made as a result of this audit
> was a one-line documentation fix** (a stale flag reference in `CLAUDE.md`). The real,
> actionable debt is structural (§5, §7), not deletable files.

---

## 2. Methodology

"Active" was determined by combining four independent signals, so that no single
blind spot (e.g. coverage under-counting subprocess entrypoints) drives a conclusion:

1. **Static import graph** — a module is reachable if some other first-party module
   imports it (directly or through a package `__init__`).
2. **Out-of-band invocation** — entrypoints declared in `pyproject.toml` `[project.scripts]`,
   modules run via `python -m mac.<mod>` from the `Makefile`/`deploy/`/`scripts/`, and app
   factories mounted by the API server. These are "active" even when nothing imports them.
3. **Runtime coverage** — `coverage.json` (a real green gate run) shows which lines actually
   executed under the test suite.
4. **Vendoring metadata** — `SNAPSHOT_PIN`, the `deploy/hermes/` patch stack, and the
   `pyproject.toml` coverage `omit` rule classify the `_hermes/` tree.

**Known limits of the method.** Coverage under-attributes code that only runs in a *separate*
process the tests spawn but do not trace (the clearest example is `git_askpass.py`, a git
credential helper git invokes as a subprocess — it shows 0% yet is not dead). Dynamic dispatch
(e.g. the vendored Hermes CLI dispatching into tools by name) is invisible to a static import
graph. Both limits are called out where they affect a conclusion, and no module is labelled
"dead" on a single signal alone.

---

## 3. Active-code map

### 3.1 Entrypoints → runtime roles

All entrypoints are declared in `pyproject.toml:75-103` (there is no `setup.py`
`console_scripts` fallback — this is the single source of truth). They collapse into
**four runtime roles** plus supporting daemons:

| Runtime role | Entrypoint(s) | Module | Size |
|---|---|---|---:|
| **CLI** (the one documented interface) | `mac` | `cli.py::main` | 8,678 |
| **API / hub** (FastAPI) | run via the `mac` serve path | `api.py::create_app` (line 3926) | 9,410 |
| **Worker / executor** | `mac-agent` | `worker.py::main` | 8,070 |
| **K8s orchestrator** | `mac-k8s-orchestrator` / `-bootstrap` / `mac-task-runner` | `k8s/orchestrator.py`, `k8s/bootstrap.py`, `k8s/job_executor.py` | 3,496 (pkg) |
| Router / gateways | `mac-router`, `mac-hermes-gateway`, `mac-firecrawl-gateway` | `router_service.py`, `hermes_gateway.py`, `firecrawl_gateway.py` | — |
| Supporting daemons/CLIs | `mac-ledger-backup`, `mac-evidence`, `mac-git-askpass`, `mac-webdav-server`, `mac-openshell-supervisor`/`-collector` | various | — |

The `mac` CLI is **hub-aware**: it either talks to a hub over HTTP or opens a local SQLite
ledger via `--db`. The domain brain behind the hub is a single class, `ControlPlane`
(`services.py:1273`), instantiated by the API composition root and shared by every HTTP
handler.

### 3.2 Sub-packages (`src/mac/`)

| Package | LOC | Role | Status |
|---|---:|---|---|
| `k8s/` | 3,496 | K8s dispatcher, bootstrap, Job-pod runner | Active |
| `acp/` | 3,142 | Agent Coordination Protocol server | Active |
| `a2a/` | 1,077 | Agent-to-Agent protocol surface | Active |
| `activation_probe/` | 421 | Model activation probing | Active (low coverage) |
| `http_routes/` | 122 | *Intended* domain-router extraction | **Abandoned mid-migration** (see §5.3) |

---

## 4. Vendored runtime — `src/mac/_hermes/`

`src/mac/_hermes/` is a **vendored, integrity-pinned snapshot** of
`https://github.com/NousResearch/hermes-agent.git` (commit `b1a25404…`, vendored
`2026-05-31`), per `src/mac/_hermes/SNAPSHOT_PIN` and ADR 0001
(`docs/adr/0001-unify-hermes-runtime-into-mac.md`). It is ~443K LOC — **more than twice the
size of all first-party code** — and is deliberately quarantined:

- **Excluded from coverage** (`pyproject.toml:130-132`: "do not count vendored Hermes
  internals against this repository's threshold").
- **Kept in sync by machinery, not hand-edits** — a patch stack under `deploy/hermes/`
  (`multi-slack-mvp.patch`, `post-snapshot-mac-fixes.patch`, and others) is re-applied on
  every re-vendor, and a full-tree SHA256 digest (`deploy/hermes/HERMES_TREE_SHA256`,
  enforced by `tests/test_hermes_vendor_integrity.py`) fails the build on any drift.

### 4.1 First-party code touches Hermes through exactly one door

No first-party module imports `mac._hermes.*` directly. Instead
`src/mac/hermes_vendor.py::ensure_on_path()` puts the tree on `sys.path`, after which the
adapter layer imports Hermes' flat top-level `hermes_cli` package at just **two** sites:

- `src/mac/hermes_gateway.py:146` — `from hermes_cli.main import main as _cli_main`
- `src/mac/hermes_config_surface.py:237` — `from hermes_cli import config as hermes_config`

Everything else in `_hermes` (`agent`, `gateway`, `tools`, `plugins`, `skills`,
`providers`) is reachable **only transitively at runtime**, when `hermes_cli.main.main()`
dispatches into it *and* the relevant feature is configured.

### 4.2 Active-through-one-door vs. dormant vendored surface

| Submodule | LOC | Reachable from a MAC entrypoint? |
|---|---:|---|
| `hermes_cli` | 110,926 | **Yes — the only direct entry.** |
| `gateway` | 82,575 | Transitive; only Slack/Telegram paths wired by MAC config. |
| `tools` | 73,400 | Transitive (agent tool dispatch). |
| `agent` | 68,043 | Transitive. |
| `plugins` | 50,783 | Transitive; dashboards/dist pruned at vendor time. |
| `skills` | 13,078 | Transitive. |
| `tui_gateway` | 7,975 | **Dormant** — no MAC reference. |
| `acp_adapter` | 4,955 | Dormant (MAC has its own `acp/`). |
| `cron` | 3,320 | Dormant. |
| `providers` | 389 | Transitive (provider decision is MAC-patched). |

The `gateway/platforms/` directory ships ~25 backends (feishu, yuanbao, wecom, weixin,
dingtalk, matrix, signal, whatsapp, email, sms, qqbot, bluebubbles, homeassistant, …), but
MAC's own config only ever enables `{hermes, slack, telegram}`
(`notifier_service.py:53 SUPPORTED_CHANNEL_TYPES`; `k8s/config_loader.py:209`). The rest is
**dormant vendored surface carried for upstream fidelity, not MAC-reachable code.**

> **Audit stance on `_hermes`:** this is *correctly managed* dead-ish surface. It is not
> repository rot — it is an intentional, integrity-pinned upstream mirror. It should **not**
> be hand-pruned (that breaks the digest guard and the re-vendor workflow); prune it upstream
> in the vendor script if at all. It is out of scope for the companion Haskell migration study
> for the same reason.

---

## 5. Duplication & overlapping implementations

This is where months of iteration show. None of these are outright forked "v1/v2"
implementations — the repo is clean on that axis (no `_old`/`_v1`/`_deprecated`
**filenames** exist). The duplication is subtler: **half-finished decompositions and
colliding vocabularies.**

### 5.1 `services.py` — a facade layered over a half-extracted core

`ControlPlane` (`services.py:1273`, the file's **only** class, **719 methods**,
**26,433 lines**) simultaneously plays two roles:

- **A delegation facade** over ~40 already-extracted service classes wired in `__init__`
  (`services.py:1367-1405`): `TaskLedgerService`, `DispatchService`, `IdentityService`,
  `HumansService`, `ObservabilityService`, `AgentBusService`, `WorkflowService`, and more
  (imports at `services.py:162-245`). Hundreds of methods are one-line pass-throughs — e.g.
  `register_tenant`/`get_tenant`/`list_tenants` (`services.py:1992-1999`) just call
  `self.identity.*`.
- **A ~15K-line un-extracted core** — the task-lifecycle, lease, agent/attestation, dispatch,
  and default-review engines still live *inside* the class as methods (see §8, P3).

So the same operation frequently exists in two places (facade method **and** service class).
This is not harmful behavioural duplication, but it means the file is *mostly a delegation
shim wrapped around a still-monolithic core*, and its `__init__` comment
(`services.py:1367-1368`) explicitly states the intent: *"New domains should land here as
their own service classes rather than as more methods on ControlPlane."* The decomposition is
sanctioned and half-done.

### 5.2 "Dispatch" means three different things

The word *dispatch* names three distinct concerns, a real readability hazard:

- **Authority** — the real dispatch engine lives in `services.py`: `_dispatch_batch_impl`
  (17546), `_claim_next_for_agent_impl` (17881), `dispatch_once` (17521),
  `claim_next_for_agent` (17856).
- **Thin wrapper** — `task_lifecycle.py:138 DispatchService` forwards `dispatch_once` /
  `claim_next_for_agent` straight back to the `ControlPlane`.
- **Client transport** — `dispatch.py` (3,253 lines) is an entirely different concern:
  `LocalDispatch` (135), `RemoteDispatch` (276), `_RemoteStore` (252) — the *client-side* hub
  transport. Same vocabulary, opposite side of the wire.

### 5.3 Two HTTP route-registration paths

- **Primary (monolith):** `api.py::create_app` (line 3926) contains **403 inline route
  decorators** (`@app.get/post/…`) defined as closures — dashboard, tenants, humans,
  hermes-instances, and the inline A2A (`api.py:4352`) and ACP (`api.py:4382`) mounts.
- **Secondary (abandoned):** the `http_routes/` package is the *start* of a domain-router
  extraction that never progressed — it contains only `system.py` (121 lines,
  `build_system_router` exposing `/health` and `/repository-refs/reconciler`), included once
  at `api.py:4310`. Its `__init__.py` docstring ("Domain-oriented FastAPI router factories")
  is a signpost to an intended-but-stalled migration.

### 5.4 Ledger- and ticketing-adjacent sprawl

Not parallel implementations, but a fragmented surface around one concern worth consolidating:

- **Store/ledger:** `store.py` (`Store` protocol + `SQLiteStore`) and `store_postgres.py`
  (`PostgresStore`) are two clean backends behind one protocol. Around them accrete
  `task_ledger_audit.py`, `ledger_backup.py`, `ledger_backup_scheduler.py`,
  `local_ledger_migration.py`, `migration.py`, and `work_package_store.py`;
  `TaskLedgerService` (`task_lifecycle.py:26`) adds yet another thin layer (outbox helpers).
- **Ticketing trio:** `ticketing.py` (connector abstraction), `ticketing_service.py`
  (`TicketingCoordinator`), and `tickets_mirror.py` (the `.tickets/<id>.md` emitter). This is
  the intentional "ledger vs. local mirror" split from `CLAUDE.md`, but the boundary between
  `ticketing.py` and `ticketing_service.py` is thin.

---

## 6. Dead / orphaned code

Genuine dead code is **small** — and a verification pass shrank it almost to nothing. The
initial static scan produced two dead-code hypotheses; both largely failed verification. They
are recorded here honestly, *including the correction*, because the corrected version is the
one that should guide any action.

### 6.1 "Orphaned" modules — verified as staged capabilities, NOT dead code

Nine modules are imported by no `src/mac` module and are reachable, on a static import graph,
only from their own test files: `dream_scanner.py`, `predispatch_conflict.py`,
`investigation_artifacts.py`, `openclaw_checkpoint_gc.py`, `hgx_provision.py`,
`skill_auto_repair.py`, `project_inception.py`, `harness_reflex.py`,
`evidence_reuse_verifier.py`. The initial hypothesis was that these were accretion residue to
remove. **Checking each against the reference graph and git history overturned that:**

- **`project_inception.py` is actively used** — imported and invoked by
  `scripts/prove-c26-inception.py` (`run_c26_project_inception_proof`). Not orphaned at all.
- **`evidence_reuse_verifier.py`, `harness_reflex.py`, `investigation_artifacts.py`,
  `dream_scanner.py` are referenced from other first-party modules** — via docstrings/design
  comments in `services.py:9476`, `models.py:2990/2998`, `harness_recovery.py:4`,
  `dream_repair_tasks.py:22`, and via the test-impact map (`scripts/resolve-impacted-tests.py`).
  They are part of the documented design surface, not stray files.
- **The remaining four are recent, deliberate additions, tested-first, integration pending:**
  `openclaw_checkpoint_gc.py` landed in the **current HEAD commit (2026-07-24)**,
  `hgx_provision.py` (2026-07-23), `predispatch_conflict.py` (2026-07-16),
  `skill_auto_repair.py` (2026-07-12). Deleting these would undo work committed this week.

> **Corrected conclusion:** this is not dead code — it is a deliberate **"land the capability
> with unit tests, wire it into the runtime later"** pattern typical of an autonomous-fleet
> codebase. **No deletion is warranted.** The only legitimate follow-up is a *tracking* one:
> confirm each staged module has a real integration path on the roadmap, so a genuinely
> abandoned one doesn't hide among the merely-not-yet-wired. (Contrast `ide_launcher.py` /
> `hermes_chat_config.py`, un-imported yet invoked via `python -m mac.<mod>` — also not dead.)

### 6.2 Beads-removal residue — mostly a live mechanism with a legacy name

The legacy beads *dolt-sync* bridge is genuinely gone (removal markers at `dispatch.py:2144`,
`api.py:8745`). But two of the three "residue" items flagged initially turned out to be **live
code**, not scar tissue:

- **Real and fixed:** the flag `MAC_BEADS_BRIDGE_ENABLED` named in `CLAUDE.md` has **zero code
  references anywhere in the repo** — it existed only in prose. This documentation staleness
  was the one real, safe finding, and it has been corrected in `CLAUDE.md`.
- **NOT dead (correction):** `sync_beads` is **not** disabled plumbing. It defaults to `True`
  in `services.py` and gates a live call — `if sync_beads: self.drain_task_transition_outbox(...)`
  at `services.py:10911`. The name is a legacy artifact, but the behaviour it controls
  (synchronous draining of the task-transition outbox) is active, exercised by 70+ tests, and
  part of the remote contract (`test_dispatch_remote_contract.py`). The `dispatch.py:588`
  client only declines to *forward* it — remote claims are intentionally local-drain-only.
  **This is a rename opportunity (§8-P2), not a deletion.**
- **NOT dead (correction):** `bridge_repository_register` is a **live CLI command** with a real
  handler (`cmd_bridge_repository_register`, `cli.py:4319`). Only its default actor string
  `"beads-bridge"` (`cli.py:7906`) is a cosmetic legacy label.
- **Legitimately retained:** `beads_migrator.py` + `cmd_task_migrate_beads` /
  `cmd_task_detect_beads` (`cli.py:1438-1465`) — the one-way import tool, correctly kept.

### 6.3 Zero-coverage file (not actually dead)

Exactly **one** first-party file is at 0.00% coverage: `git_askpass.py` (29 statements). It
is the `mac-git-askpass` credential helper that **git** runs as a subprocess; the tests that
exercise it do so out-of-process, so coverage never attributes it. This is a
coverage-attribution gap, **not** dead code — listed here only to preempt a false positive.

### 6.4 Inline "legacy" handling

There are no legacy *files*, but there is inline legacy *logic*: e.g.
`_reconcile_legacy_task_state_semantics` (`services.py:1613`), and ~49 "legacy"/"deprecated"
markers in `services.py`, ~22 in `cli.py`, ~21 in `store.py`, ~18 each in `api.py` and
`publication_lane.py`. These are compatibility shims to review opportunistically, not
deletion targets on their own.

---

## 7. Test-suite health

The test suite is the **largest single maintenance cost in the repository** and deserves
first-class attention.

### 7.1 Size and shape

- **445 files, ~200,600 LOC, 6,883 `def test_` functions → 8,555 collected nodes.**
- Organization is **marker-based, not directory-based**: `tests/conftest.py`
  auto-applies markers by path (`api`, `cli`, `ui`) and by filename cluster (`fleet`,
  `work_package`, `worker`, `heavy_e2e`), registered in `pyproject.toml:108-120`. An operator
  can disable whole clusters via `MAC_TEST_DISABLE_GROUPS`.
- **`scripts/run-contract-tests.sh` (506 lines) is *the* mandated quality gate** (`CLAUDE.md`
  "Session Completion"). It builds a hermetic env (unsetting all `MAC_*`/`HERMES_*`/`SLACK_*`
  and provider keys), runs a two-phase xdist-parallel + serial split, merges coverage across
  child processes, and enforces the floors in `test-policy.toml` (**90% statement / 80%
  branch**) via `scripts/coverage-policy.py`.

### 7.2 Runtime cost (measured)

From `.test-portfolio/timings.json` (a real green run, exit 0):

- **8,555 nodes, ~2,052 seconds (~34 minutes) of serial CPU time.** xdist fans this out to a
  fraction of wall-clock, but **coverage tracing roughly halves throughput**, so the
  coverage-on gate is materially slower than the `MAC_TEST_COVERAGE=0` fast path.
- Overhead concentrates in real I/O: **subprocess in 137 files**, FastAPI `TestClient` in 45,
  `threading.Thread` in 29, `time.sleep` in 22. Docker (17), Kubernetes (11), and Postgres (7)
  tests are gated behind opt-in markers/`importorskip`, so the default run skips them.
- The slow tail is e2e/subprocess-heavy: `test_documentation_book.py` (42s),
  `test_fleet_cohort_transaction.py` rollback tests (8–15s each), `test_worker_process_e2e.py`
  (~8s).

### 7.3 Test duplication (real, and costly)

- **37 `_edges.py` coverage-companion modules**, 16 of them with a same-named base twin (both
  `test_X.py` and `test_X_edges.py`) — e.g. `test_gitops`, `test_hermes_runtime`,
  `test_k8s_bootstrap`, `test_workflow_service`. Their names advertise that they exist to hit
  the 90/80 floors. Prime candidates to fold back into their base module and parametrize.
- **Under-parametrized:** only **111 of 445** files use `@pytest.mark.parametrize`; **326 have
  none**. 6,883 functions expand to only 8,555 nodes — most tests are single-case.
- **Monoliths:** `test_control_plane.py` alone is **14,278 LOC** (7% of the entire test tree);
  the top six files are ~34K LOC combined — the likeliest home of near-identical test bodies.

> This test structure is the direct downstream cost of the §5.1 god-class: because
> `ControlPlane` is one enormous stateful object, testing it drives the 14K-line
> `test_control_plane.py` monolith and the `_edges` companions that chase its branch coverage.
> **Decomposing `services.py` and consolidating the tests are the same project viewed from two
> ends.**

---

## 8. Prioritized recommendations

All recommendations are *written analysis only* — no changes were made. Each carries a risk
note and a verification step. Ordered by value-to-risk.

### P1 — Documentation & tracking hygiene *(done / low risk)*

- **Done:** corrected `CLAUDE.md`'s reference to the nonexistent `MAC_BEADS_BRIDGE_ENABLED`
  flag (§6.2). This was the only change the audit's dead-code line of inquiry actually
  justified.
- **Do NOT delete the §6.1 modules** — verification showed they are recent, tested, staged
  capabilities (one in the current HEAD commit), and `project_inception.py` is in active use.
  The correct follow-up is a *tracking* task: record the intended integration path for each
  staged module so a truly-abandoned one can't hide among the not-yet-wired.
- **Verify:** `CLAUDE.md` is prose-only; no test impact.

### P2 — Disambiguate names and finish stalled decompositions *(low–medium risk)*

- **Rename `sync_beads` → `drain_outbox`** (or similar). It is live, not dead (§6.2), so this
  is a rename, not a removal — but it touches ~5 `services.py` signatures, the `dispatch.py`
  client, and **70+ test call sites**, so it must run the full gate. Retire the cosmetic
  `--actor default="beads-bridge"` (`cli.py:7906`) in the same pass.
- Rename the three "dispatch" layers (§5.2) to distinct names (e.g. `DispatchEngine` in
  services, `DispatchFacade` in `task_lifecycle`, `DispatchClient`/`HubTransport` in
  `dispatch.py`). Mechanical, high readability payoff, but wide blast radius.
- **Decide** `http_routes/`: either complete the domain-router extraction from
  `api.py::create_app`, or delete the stub and mount `system.py`'s routes inline. Leaving it
  half-done is the worst state.
- **Verify:** full `scripts/run-contract-tests.sh` green; route inventory unchanged
  (`test_api_route_coverage.py`); no import cycles. These are mechanical but broad — they are
  *scoped refactors that need the gate*, not unattended one-liners.

### P3 — Extract the five un-extracted `ControlPlane` blocks *(high value, medium risk)*

Following the pattern the code already sanctions, extract into service classes:
**task-transition** (~9720–10500), **lease authority** (~12688–14148),
**agent/attestation/dispatch-hold** (~14547–16171), **the dispatch engine** (~17521–18188),
and **the default-review engine** (~19159–20940, 23644–25868). Each is internally cohesive and
mostly self-referential, keeping extraction risk moderate. Drop
`_reconcile_legacy_task_state_semantics` (`services.py:1613`) and the `sync_beads` residue
during this work.
- **Verify:** behaviour-preserving refactor — `test_control_plane.py` and the `_edges`
  companions pass unchanged before any test consolidation.

### P4 — Consolidate the test suite *(high value, ongoing)*

- Fold the 37 `_edges` companions into their base modules as parametrized cases (§7.3).
- Parametrize the 326 single-case files where scenarios repeat; break up the 14K-line
  `test_control_plane.py` along the new §P3 service boundaries.
- Target: **less total test LOC and less wall-clock at equal or better coverage** — directly
  cutting the ~34-minute-per-change gate cost.
- **Verify:** coverage floors (`test-policy.toml`) hold; the `portfolio` guard confirms no
  unique line/arc coverage is lost when a test is retired.

---

## 9. Definition of "active code" (carried into the migration study)

For the companion `docs/haskell_migration.md`, **"active code" = essentially the whole ~185K
LOC of first-party `src/mac/` code excluding `_hermes/`** — the §6 verification found no
material dead residue to subtract (only a stale doc reference, now fixed). The vendored
Hermes runtime is *out of scope* for migration: it is a pinned upstream mirror whose whole
value is staying faithfully in sync with `NousResearch/hermes-agent`, and porting it to
another language would forfeit that. The migration study costs the active first-party surface
and treats Hermes as a stable external runtime behind a process boundary.
