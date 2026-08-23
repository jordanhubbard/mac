# ADR 0028: Installation is a verified package plus enrollment, not a push

- Status: Proposed
- Date: 2026-08-22
- Decision owner: MAC fleet owner
- Related: [ADR 0015](0015-macos-nodes-are-host-installs.md) — macOS nodes are
  host installs, so a host-install artifact is required and a container image
  cannot substitute for one
- Related: [ADR 0027](0027-upgrades-are-versioned-and-fail-closed.md) — a
  package flip changes code, not schema; §3 and §6 of that ADR still govern the
  store
- Related: [ADR 0009](0009-minimal-base-on-demand-layered-provisioning.md) —
  the same "verify, cache, reuse" discipline, applied to the node instead of
  the sandbox

## Context

mac has no installation artifact. It has a deployment *procedure*: an operator
runs the fleet deploy script, which SSHes to each node and executes a long
chain of host-mutating steps under a cohort transaction.

### What exists, measured 2026-08-22

- `deploy/deploy-mac-fleet.sh` is **16,196 lines**; the payload it uploads and
  runs on the target, `deploy/fleet-node-install.sh`, is **14,383 lines**. Just
  over **30,000 lines of shell** execute on a node per deploy.
- That shell names three supervisors inline — 257 `launchd`, 142 `systemd`,
  97 `supervisord` references in the node installer alone — because the
  procedure, not an artifact, is what knows how to start mac on each platform.
- `fleet-node-install.sh` contains **115** `restore` references, and there is a
  dedicated `deploy/fleet-node-rollback-supervisor.py` with a fail-closed
  protocol. Restore contracts exist because the deploy mutates the node in
  place and must be able to reconstruct what it overwrote.
- `make package` produces exactly two artifacts: one `dist/mac-*.whl` and
  `dist/mac-hub-ui.tar.gz`. Neither is per-platform, and `make publish` is an
  alias for `package-cli` that explicitly does not upload.
- `.github/workflows/ci.yml` triggers on push to `main`, pull request,
  a nightly schedule, and `workflow_dispatch`. There is **no tag trigger and no
  release job**. Nothing has ever been published as a release asset.

So there is no version of mac a person can install. There is only a fleet
owner's laptop with a checkout, and write access to every node.

### Why the procedure keeps failing on healthy hosts

Three consecutive deploy blockers were reported this session on nodes that were
themselves fine:

1. a vestigial container runtime that failed daemon-resource quiescence;
2. reviewed CodeGraph `v1.5.0` missing while nodes carried `v1.1.6`, with
   stale caches whose checksums no longer matched;
3. a dangling `src/mac/_hermes` path left behind by an earlier merge.

The repository corroborates the shape of each: `deploy/reviewed-tool-assets.sh`
does pin CodeGraph at `v1.5.0` with per-platform SHA-256 digests;
`deploy/install-qdrant-service.sh` does probe `podman` before `docker` as a
container runtime; `src/mac` carries `hermes_*.py` modules with no `_hermes`
directory. The failures were not verified first-hand here, and the point does
not depend on their details.

The point is what they have in common. **None of them is a property of the
node.** Each is a property of the *procedure's assumptions about* the node —
what was left on disk last time, what version a previous run cached, what a
merge did to the source tree the procedure copies. A push deploy's correctness
is a function of accumulated host history, and host history is unbounded.

An install of a self-contained, checksum-verified artifact has no such
function. It either extracted or it did not.

### The second cost: there is no way to be a user

Push deploy assumes the deployer holds SSH write access to every node and a
source checkout of the exact right revision. That is a description of the
author's own fleet, not of a product. Someone else's node — one they own and
that the fleet owner cannot log in to — cannot run mac today at all. ADR 0027
was written for "the moment someone else deploys"; this ADR is about that
person being able to get the software in the first place.

## Decision

### 1. Two packages, four platforms, one role flag

The release artifact family is:

    mac-server-<version>-<os>-<arch>.tar.gz
    mac-client-<version>-<os>-<arch>.tar.gz

over the four supported tuples `darwin/arm64`, `darwin/amd64`,
`linux/amd64`, `linux/arm64` — eight artifacts, plus a manifest and a checksum
file per release.

The split is by **payload**, not by role:

- **SERVER** carries everything needed to run the control plane *or* a worker:
  the mac wheel, a pinned interpreter, the hub UI bundle, supervisor unit
  templates, and the reviewed-tool pin manifest.
- **CLIENT** is thin: the CLI, a sample `~/.mac` configuration, and the global
  plugin registration that makes local projects mac-aware. No hub UI, no
  supervisor units installed by default.

**Hub versus worker is a post-install role flag on the SERVER package**, not a
separate artifact and not a separate script. This is what the original
requirement asked for and it is preserved exactly: a hub and a worker install
byte-identical trees and differ only in `~/.mac` configuration and which
supervisor units are enabled. The SERVER/CLIENT split is a different axis —
"does this machine run a mac service at all, or does it only talk to one" —
and it is justified by size and by attack surface, not by role.

### 2. A package contains code; it never contains a fleet

In the package: the wheel, the interpreter, the UI bundle, supervisor unit
*templates*, static default configuration, and the version manifest.

Never in the package: fleet topology (`fleets.yaml`), bearer tokens, provider
API keys, agent identity, soul, or memory. Those live in `~/.mac` and the
existing vault/environment flow, and they survive install, upgrade, downgrade
and uninstall untouched.

The test of the rule is mechanical, and it is the reason to state it this way:
**two operators installing the same version get byte-identical trees.** If any
per-fleet fact could reach the payload, the published SHA-256 would not be a
meaningful thing to verify. So the release build must fail closed on secret
material in the payload rather than trusting authors to keep it out.

### 3. Detect the platform before downloading anything

`install.sh` resolves `os/arch` first, from `uname`, and fails with the named
unsupported tuple *before* any payload is fetched. The normalization already
exists and is reused rather than rewritten: `mac_reviewed_platform()` in
`deploy/reviewed-tool-assets.sh` already maps `uname -s`/`uname -m` onto
exactly `{linux,darwin}` × `{amd64,arm64}` and already fails closed on anything
else.

Order is: **detect → fetch manifest → verify → fetch package → verify →
extract → configure role → enroll.** Each step's failure is named and stops the
install with the previous generation untouched.

### 4. Enrollment stays pull-flavored, and it already exists

A package install leaves the node **installed and inert**: no fleet, no
credentials, nothing claimed. Joining a fleet is a separate, node-initiated
act.

That lifecycle is not hypothetical and does not need designing. `mac admin
login`, `mac admin login status|renew`, `mac admin logout --revoke`, `mac admin
client enroll|renew|revoke|list` and `mac admin fleet ssh-spec` are implemented
in `src/mac/client_login.py` and specified in `docs/client-bootstrap-contract.md`
— scoped, independently revocable principals; a bearer token held in a separate
mode-`0600` credential record; portable profiles. Enrollment SSHes *to the hub*
to obtain a credential. It never pushes code to the node.

The consequence worth stating plainly: **the provisioner no longer needs write
access to the node's filesystem.** That is the property that makes a fleet of
machines the fleet owner does not administer possible at all.

### 5. Packages inherit the reviewed-asset checksum discipline

The release publishes a manifest — name, version, os, arch, size, SHA-256 per
artifact — and signs it. `install.sh` verifies the manifest signature, then
verifies the package digest before extraction, and caches verified packages
under `~/.mac/cache/reviewed-assets` beside the existing reviewed `uv`, Python
and CodeGraph archives. Verification is repeated on cache reuse, because the
existing onboarding checklist already learned that a cached asset is not a
trusted one.

A digest mismatch keeps the current generation, refuses the install, and names
the offending file — the same remediation the checklist already prescribes for
a reviewed-tool mismatch.

One honest caveat about `curl … | bash`: the installer entrypoint itself is
fetched over TLS and executed unverified. That is a real trust anchor and
pretending otherwise would be worse than naming it. It is bounded by keeping
`install.sh` small and inert — it detects, fetches, verifies, extracts, and
delegates; it holds no credentials and installs nothing it has not verified —
and by publishing the installer's own SHA-256 in the release notes for
operators who prefer to fetch, check, then run.

### 6. Generations on disk replace push-time restore contracts

Install extracts to a versioned directory and flips a `current` symlink;
the supervisor restarts against the new target. The previous generation stays
on disk (bounded retention, oldest pruned).

Rollback is then a symlink flip and a restart — a local, single-host operation
that needs no coordination and no reconstruction. `mac uninstall` removes
generations and leaves `~/.mac` alone unless explicitly told to purge it.

This is what retires the 115 restore call sites: **a package install never
overwrites, so there is nothing to restore.** The restore machinery exists to
undo in-place mutation, and there is no longer in-place mutation to undo.

Two things a symlink flip does **not** roll back, and they must not be assumed:
database schema and agent-owned state. Downgrading a node whose hub has already
migrated is precisely ADR 0027 §3's fail-closed case, and it stays fail-closed.
Code generation and schema version are different clocks.

### 7. CI publishes packages on a release tag

A tag-triggered job builds the eight artifacts, generates the manifest and
checksums, and attaches them to the GitHub release. This job does not exist
today in any form, so it is net-new work rather than a modification.

The build matrix is real, not ceremonial: the wheel and UI bundle are
platform-independent, but the bundled interpreter is not, so darwin artifacts
must be assembled on darwin runners. A release that cannot produce all eight
artifacts publishes none of them — a partial artifact set is how an operator
ends up installing a version their peer cannot.

### 8. Push deploy keeps working, and retires per phase against named facts

Both mechanisms exist during the transition, and a node uses exactly one.
The fleet registry records the node's install method, and the deploy script
**refuses to push to a package-managed node** rather than racing it. Ambiguity
about which mechanism owns a host is the failure mode to design out first.

Retirement is stated as conditions, not dates. Each phase retires when the
package demonstrably subsumes it:

- **prerequisites** — when the package carries every prerequisite the phase
  installs, verified on a fresh host of each supported platform;
- **phase-1 quiesce** — when generation-flip has replaced in-place mutation, so
  there is no window during which the node is half-updated to quiesce for;
- **restore contracts** — when the previous generation is provably present on
  disk after every install (§6);
- **phase-2 arm/apply** — when supervisor unit templates ship in the package
  and role configuration enables them locally.

What does **not** retire is the cohort transaction. A fleet is still N nodes
that must cut over together, and ADR 0027 §6 already says an upgrade is a fleet
event. `deploy-mac-fleet.sh` shrinks from *the thing that installs mac* to *the
thing that coordinates when N nodes each run their local upgrade verb* — which
is a few hundred lines of orchestration, not thirty thousand lines of shell.

### 9. Kubernetes shares the payload, not the installer

`deploy/k8s` runs stateless `mac-api` and `mac-runner` Deployments against an
externally managed Postgres. Those are OCI images: immutable, no installer, no
symlink generations, no SSH enrollment.

The rule is that the container image and the host package are **built from the
same payload at the same version** and are described by the same manifest — one
artifact family, two delivery mechanisms — but they do not share an installer,
and the k8s path does not grow one. Host installs get `install.sh`; k8s gets an
image tag and its own reconciliation.

### 10. The installer menu names all three roles and offers cancel

With `--role hub|worker|client` the installer is non-interactive and scripted
end to end. With a TTY and no role, it presents a menu: **hub, worker, client,
cancel**. Cancel is an explicit choice, and it is what happens on EOF or on a
non-TTY invocation with no role — an installer that guesses a role when nobody
answered has picked the most consequential setting by accident.

Choosing **client** warns, before doing anything, that a client requires an
already-running hub and is useless without one, and collects the hub endpoint
as part of the flow rather than leaving a configured-looking install that
cannot reach anything.

Re-running the installer at the same version and role is a **no-op that exits
zero** and prints the current generation. Idempotence is what makes an
installer safe to put in configuration management and safe to tell a confused
operator to run again.

## Consequences

- mac becomes installable by someone the fleet owner has no access to. That is
  the precondition ADR 0027 assumed and nothing yet delivered.
- A class of deploy failure disappears rather than being fixed: stale caches,
  leftover paths, and vestigial runtimes stop being able to break an install,
  because the install stops reading the host's history.
- The project acquires a release process it does not have, with real
  obligations: signing key custody, a per-platform build matrix, and the rule
  that a partial artifact set is not published.
- Roughly 30,000 lines of shell become deletable, in the staged order of §8.
  They are not deleted by this ADR, and deleting them before the criteria are
  met would be the same mistake in the other direction.
- Two delivery mechanisms coexist for a period, which is strictly more surface
  than one. §8's registry-level exclusivity is what keeps that from becoming a
  fleet where nobody can say how a given node got its code.
- `curl | bash` is accepted as the entrypoint, with the trust anchor named in
  §5 rather than argued away.

## Alternatives considered

**Keep push deploy and fix the blockers.** Rejected. Each blocker was fixable,
and fixing them does not reduce the space of future blockers, because that
space is "everything that has ever happened to the host."

**Native OS packages (`.pkg`, `.deb`, `.rpm`) instead of tarballs.** Not
rejected, deferred. They give the platform's own uninstall and dependency
handling, and they cost per-format tooling, a repository to serve them, and —
on macOS — notarization, which a downloaded tarball does not require. Tarball
plus generations is the smaller first step and does not foreclose them; the
manifest and checksum discipline of §5 is what a native package would reuse.

**`pip install mac`.** Rejected as the primary path. It delivers the wheel and
nothing else — no UI bundle, no pinned interpreter, no supervisor units — and
inherits whatever Python the host happens to have, which is the class of
host-history dependence this ADR exists to remove. It remains reasonable for
library consumers.

**One package with the role baked in.** Rejected: it doubles the artifact
matrix from eight to sixteen for a difference that is configuration, and it
makes converting a worker to a hub a reinstall.

**A single package with no SERVER/CLIENT split.** Rejected: it ships the hub
UI, the interpreter and the supervisor units to laptops that only run the CLI,
and the enrollment story for a workstation is genuinely different from a
node's.

## Open questions

These are deliberately not settled here, because they are implementation
choices that this ADR's constraints should discipline rather than precede:

- Signing key custody and rotation — who holds it, and how an operator learns
  the key changed.
- Install root: a system path such as `/opt/mac/versions/<version>` versus a
  user-owned `~/.mac/versions/<version>`. This interacts with §2's rule that
  `~/.mac` holds configuration, and with whether install requires privilege.
- macOS quarantine and notarization behaviour for the downloaded tarball,
  confirmed on a real host rather than assumed.
- Generation retention depth, and whether it is a count or a floor of "at least
  the last known-good".
