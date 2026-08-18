!!! warning "Historical field note"
    This design/spec note is retained for provenance only. It is not a current operating contract; the premises or implementation path it describes have been superseded or never shipped.

# A Notional Haskell Migration Plan for MAC

*A purely theoretical exercise. Nothing here is a recommendation to actually rewrite the
system; it is a comprehensive estimate of what such an effort would require and a reasoned
verdict on whether it would pay off. It builds directly on `docs/audit.md`, which defines the
active code that would be in scope. No source, test, or configuration file was modified in
producing this document.*

---

## 1. Framing & scope

### What would be migrated

Per the companion audit, **"active code" is the ~185K LOC of first-party `src/mac/` code
(excluding the vendored `_hermes/` tree), minus the confirmed-dead residue.** That is the
target of this study.

### What would *not* be migrated

- **The vendored Hermes runtime (`src/mac/_hermes/`, ~443K LOC).** It is a pinned,
  integrity-guarded snapshot of `NousResearch/hermes-agent`, kept faithful to upstream by a
  patch stack and a tree digest. Its entire value is *staying in sync with upstream Python*.
  Porting it to Haskell would fork it permanently off its own upstream and forfeit that value.
  In a migrated world, Hermes **stays a Python process** that a Haskell MAC launches and talks
  to across a process boundary — exactly the loose coupling the audit found already exists (a
  single `hermes_cli` entry door via `hermes_gateway.py` / `hermes_config_surface.py`).
- **The Python test corpus**, except insofar as its *intent* must be re-expressed in Haskell
  (see §5 — this is the crux of the cost/benefit question).

### Why this coupling is the key architectural fact

Because MAC already shells into Hermes through one narrow door, the migration boundary is
clean: a Haskell MAC would spawn `hermes` as a subprocess (or over its existing HTTP/socket
gateway) and never need Haskell↔Python FFI. **The hard integration problem is avoided by the
existing architecture, not by anything the rewrite would add.**

---

## 2. Target Haskell stack & library mapping

The migration difficulty of each area is dominated by whether its Python dependencies have a
mature Haskell counterpart. The current dependency set (`pyproject.toml:12-39`) maps as
follows:

| Python dependency | Role | Haskell counterpart | Maturity / risk |
|---|---|---|---|
| `fastapi` | HTTP API framework | **`servant`** (+ `servant-server`) | Excellent — types-first routing is a *strength* here |
| `uvicorn[standard]` | ASGI server | **`warp`** | Excellent |
| `pydantic` | Validation / (de)serialization | **records + `aeson`** | Good — types replace runtime validation |
| `httpx` / `requests` | HTTP client | **`http-client`** / `req` | Good |
| `openai` (2.24) | LLM provider SDK | **hand-rolled `aeson` client** | **Weak** — no maintained SDK; must build & track API drift |
| `cryptography` / `PyJWT[crypto]` | Crypto / JWT / HMAC | **`crypton`** (ex-`cryptonite`) / `jose` | Good — HMAC/signing parity is achievable but must be bit-exact |
| `psycopg` + `psycopg_pool` | Postgres | **`hasql`** / `postgresql-simple` | Good |
| SQLite (stdlib `sqlite3`) | Local ledger store | **`direct-sqlite`** / `sqlite-simple` | Good — but transaction semantics must match (§4) |
| `numpy` | Numeric (agent compute) | **`hmatrix`** / `massiv` | Good, but a different idiom; usage here is light |
| `jinja2` | Templating | **`mustache` / `ede`** | **Weak** — no drop-in Jinja2; templates need porting |
| `fire` | CLI arg parsing | **`optparse-applicative`** | Excellent (and safer) |
| `croniter` | Cron schedules | **`cron`** | Good |
| `psutil` | Process/host introspection | **`unix` / shelling out** | Fair — platform-specific gaps |
| `kubernetes` (dev/k8s extra) | K8s API client | **`kubernetes-client` (immature)** | **Weak** — likeliest to require hand-rolled REST + codegen |
| `prompt_toolkit` / `rich` | TUI / terminal UI | **`brick` / `haskeline`** | Fair — different model; only if TUI is in scope |
| `slack-sdk` / `slack_bolt` | Slack | **hand-rolled** or via the retained Hermes gateway | Weak — but largely delegable to Hermes |

**Three dependencies are the difficulty multipliers:** the **OpenAI client**, the
**Kubernetes client**, and **Jinja2 templating** — each lacks a mature Haskell equivalent and
would be built or hand-rolled.

Target toolchain: **GHC 9.x**, Cabal or Stack, `servant`/`warp` for the hub, `hasql`/
`direct-sqlite` for storage, `async`/`stm` for concurrency, `typed-process` for the extensive
subprocess orchestration, `hspec` + `QuickCheck`/`hedgehog` for tests.

---

## 3. Per-area difficulty estimate

Difficulty is a 1–5 scale: **1** = mechanical, **3** = substantial but well-supported,
**5** = research-grade or ecosystem-gap risk. LOC are approximate first-party sizes from the
audit.

| # | Functional area | Key modules | ~LOC | Difficulty | Dominant risk | What Haskell buys |
|--:|---|---|---:|:--:|---|---|
| 1 | **Evidence / crypto / signing** | `evidence_cli.py`, `git_askpass.py`, HMAC signers in `services.py` | ~2K | **2** | Byte-exact HMAC/attestation parity | Compile-time guarantees on signing paths; strong crypto types |
| 2 | **CLI** | `cli.py` | ~8.7K | **2** | Large surface, but arg-parsing is mechanical | `optparse-applicative` eliminates a class of arg bugs |
| 3 | **Store / ledger** | `store.py`, `store_postgres.py`, `migration.py` | ~10K | **3** | SQLite + Postgres transaction/concurrency semantics parity | Typed schema, no `None`-column surprises |
| 4 | **API / hub (HTTP surface)** | `api.py` (403 inline routes) | ~9.4K | **3** | 403 routes to restate; dynamic JSON at the boundary | **Servant makes routes type-checked** — a genuine strength |
| 5 | **Worker / executor + sandbox** | `worker.py`, `executor_sandbox.py`, `task_executor.py` | ~14K | **4** | Heavy subprocess orchestration, sandboxing, credential handling | Typed process lifecycles; `bracket` for cleanup |
| 6 | **Router / gateways** | `router_service.py`, `hermes_gateway.py`, `firecrawl_gateway.py` | ~6K | **4** | LLM streaming, no OpenAI SDK, provider drift | Explicit stream types; but SDK gap is real cost |
| 7 | **K8s orchestrator** | `k8s/orchestrator.py`, `bootstrap.py`, `job_executor.py` | ~3.5K | **4** | Immature Haskell K8s client → hand-rolled REST | Typed reconciliation loops |
| 8 | **A2A / ACP protocols** | `a2a/`, `acp/` | ~4.2K | **3** | WebSocket + protocol state machines | Types shine on protocol state machines |
| 9 | **Activation probe** | `activation_probe/` | ~0.4K | **2** | Small, numeric | Minor |
| 10 | **`ControlPlane` domain core** | `services.py` (the un-extracted ~15K) | ~15–26K | **5** | 26,433-line, 719-method stateful god-class over live SQLite transactions | The **biggest prize *and* biggest risk** — see below |

### The `services.py` problem dominates everything

Area 10 is the crux. `ControlPlane` is a single 26,433-line class holding the task-lifecycle,
lease, agent/attestation, dispatch, and default-review engines as mutually-referential methods
over a live transactional store (audit §5.1, §8-P3). Porting it means simultaneously:
(a) untangling the god-class into modules, (b) re-expressing its implicit state machine in
Haskell's explicit-effect idiom (`ReaderT`/`StateT` or an effect system), and (c) preserving
exact transactional semantics against SQLite/Postgres.

**Strategic implication:** the audit's recommended P3 decomposition (extract the five blocks
into service classes) is a **prerequisite that should happen in Python first.** Migrating a
decomposed `ControlPlane` is difficulty-4 work done five times; migrating the monolith as-is
is a single difficulty-5 gamble. *The refactor the codebase already needs is also the thing
that most de-risks a hypothetical port.*

---

## 4. Cross-cutting hard problems

1. **Subprocess orchestration everywhere.** MAC is fundamentally a process-herding system
   (workers spawn executors, git, codex runners; tests alone use subprocess in 137 files). A
   Haskell port lives or dies on `typed-process` ergonomics and `bracket`-based cleanup. This
   is *manageable* and arguably *safer* in Haskell, but it is pervasive.
2. **Dynamic JSON at the hub boundary.** Python passes loosely-typed dicts freely across the
   HTTP boundary and the ledger. Haskell forces every shape to be named. This is the **central
   tax and the central benefit** at once — more upfront modelling, far fewer shape-mismatch
   bugs after.
3. **SQLite/Postgres transaction semantics.** The ledger's correctness rests on exact
   transaction, locking, and outbox behaviour. Reproducing it in `hasql`/`direct-sqlite`
   requires careful parity testing, not just a schema translation.
4. **The vendored-Hermes bridge.** Solved by architecture (subprocess boundary, §1), not FFI —
   the single biggest de-risking fact of the whole exercise.
5. **HMAC / attestation parity.** Worker evidence signing must be byte-identical across the
   Python↔Haskell boundary during any phased migration, or every worker breaks. Requires a
   shared conformance vector suite before the first line is cut over.
6. **Ecosystem gaps.** OpenAI client, Kubernetes client, and Jinja2 templating have no mature
   Haskell equivalent and would be hand-built and maintained.

---

## 5. The test-corpus problem (the decisive factor)

This is where the exercise stops being about the 185K LOC of product code and becomes about
the **~200K LOC / 8,555-node test suite** — which, per the audit, is *larger than the product
it tests* and costs **~34 minutes of serial CPU per change** (roughly doubled under coverage).

Three hard truths:

1. **The tests are the real specification.** Six thousand-plus test functions encode behaviour
   that is *not* written down anywhere else. A rewrite cannot discard them; it must
   re-establish equivalent guarantees, or it is flying blind.
2. **Most of them cannot be mechanically translated.** The suite is dominated by *integration*
   behaviour — subprocess (137 files), real `TestClient` HTTP (45), threading (29), docker/
   k8s/postgres. These test *system* behaviour, not pure functions, so they do not port by
   syntax; they must be re-authored against the Haskell system.
3. **But Haskell's type system would delete a large fraction of them outright.** A great many
   tests — especially the 37 `_edges` companion modules and the 326 single-case,
   un-parametrized files the audit flagged — exist to chase the 90% statement / 80% branch
   coverage floors on *shape* and *None-handling* and *enum-exhaustiveness* errors. **Those are
   exactly the errors Haskell's types make unrepresentable.** In Haskell you do not write a
   test to prove a field can't be `None` or a variant can't be unhandled — the compiler proves
   it. Pair that with property-based testing (`QuickCheck`/`hedgehog`) for the genuine logic,
   and the *remaining* test surface is materially smaller and faster than today's.

**This is the strongest single argument in favour of the migration**, and it is precisely the
axis the user asked about: not "is Haskell nicer," but "would its rigor cut the test overhead
that now dominates every change." The honest answer is **yes for the coverage-floor tail, no
for the integration core** — types absorb the `_edges`-style tests but do nothing for the
subprocess/e2e tests, which are the slow part of the ~34-minute gate.

---

## 6. Effort model & phasing

### Rough magnitude

Translating ~185K LOC of stateful, I/O-heavy, subprocess-orchestrating code — *plus*
re-establishing test equivalence, *plus* hand-building three missing library layers — is, at
industry norms for a like-for-like rewrite in an unfamiliar-to-the-team paradigm:

> **Order of magnitude: 8–15 engineer-years**, low confidence, wide error bars. The dominant
> line items are `services.py`/`ControlPlane` (area 10), the worker/executor sandbox (area 5),
> and re-establishing the integration-test corpus (§5) — not the mechanical CLI/store code.

The band is wide because it hinges on team Haskell fluency (a paradigm shift, not a syntax
one) and on how much of `services.py` is decomposed *before* the port begins.

### Recommended phasing (strangler-fig)

1. **Prerequisite (in Python):** execute the audit's P3 — decompose `ControlPlane` into the
   five service classes. This de-risks the port more than any other single action.
2. **Leaf-first cutover:** port the pure/leaf tools where Haskell is strongest and risk is
   lowest — `evidence_cli`/signing (area 1), then the CLI (area 2), then the store layer
   (area 3) behind a stable interface. Establish the HMAC conformance vectors here (§4.5).
3. **Hub next:** port the API surface onto Servant (area 4), running Haskell and Python hubs
   behind a shared ledger during transition.
4. **Domain core last:** port the decomposed `ControlPlane` services (area 10) one at a time.
5. **Never port:** the vendored Hermes runtime (stays a Python subprocess), and — pragmatically
   — the K8s client path unless the ecosystem gap closes.

---

## 7. Closing verdict — is Haskell's rigor worth the cost?

**For this system, as a practical undertaking: no. As the user framed it — weighing rigor
against the test overhead specifically — the answer is more interesting than a flat no, and
worth stating precisely.**

**The case *for* is real and it is exactly the one the user intuited.** The defining pain of
this codebase is not runtime crashes — it is that **the test suite is larger than the product
and costs ~34 minutes per change**, much of it spent on coverage-floor companion tests
(`_edges` modules, single-case files) that exist to police shape, null, and exhaustiveness
errors. Haskell's type system makes that entire category of error *unrepresentable*, which
would legitimately **retire a large fraction of the test suite and shrink the per-change
gate.** That is a genuine, structural, long-term win — not a stylistic preference.

**But three facts make the total cost outweigh even that substantial benefit:**

1. **The overhead Haskell removes is the *fast* half of the suite; the overhead it doesn't
   touch is the *slow* half.** The ~34-minute cost is dominated by subprocess/e2e/integration
   tests (137 subprocess files, real HTTP, docker, k8s). Types do nothing for those — they test
   *system* behaviour, not shape. So the headline benefit (fewer tests) lands mostly on the
   cheap tests, while the expensive tests survive the rewrite intact.
2. **The rewrite risk is concentrated in the one place types help least.** `services.py` is a
   26K-line stateful transactional god-class. Porting it is difficulty-5, and its correctness
   lives in *transaction ordering and effect sequencing* — properties Haskell expresses well
   but does not verify for free. The 8–15 engineer-year cost buys a *possibly* more robust core
   at the risk of subtly breaking the exact ledger semantics the whole fleet depends on.
3. **The cheaper path to the same benefit already exists — in Python.** The audit's P3
   (decompose `ControlPlane`) + P4 (fold the `_edges` tests into parametrized cases, delete the
   orphans) attack the *same* test-overhead problem for a fraction of the cost and risk, with no
   ecosystem gaps and no team-retraining. Much of what makes the test suite expensive is the
   god-class and the coverage-chasing companions — both fixable *without* changing language.

**Where Haskell would genuinely win** is in the parts already isolated and type-shaped: the
Servant API surface, the evidence/signing layer, protocol state machines (A2A/ACP), and cron/
schedule logic. If any greenfield MAC component were being written today, Haskell (or a
strongly-typed language generally) would be a defensible choice for those. **A wholesale port
of a working, fleet-critical, subprocess-and-integration-heavy system that already has a
$path to its main pain point in its own language is not.**

**Recommendation:** treat Haskell's rigor as a design *principle* to import, not a rewrite to
execute. Capture ~80% of the described benefit by doing the audit's P3/P4 in Python — decompose
the god-class, delete the accretion residue, and collapse the coverage-floor test tail — and
reserve any actual Haskell for genuinely new, well-bounded components where the type system
pays from line one and no working system is put at risk.
