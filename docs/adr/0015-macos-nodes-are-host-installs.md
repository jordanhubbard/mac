# ADR 0015: macOS nodes are host installs, not containers

- Status: Accepted
- Date: 2026-08-17
- Decision owner: MAC fleet owner
- Supersedes: the "macOS fleet nodes via Docker" amendment to
  [ADR 0008](0008-openshell-docker-engine-runtime.md) (2026-06)
- Amends: [ADR 0012](0012-hybrid-native-steward-containerized-execution.md) —
  the containerized-execution half now applies to Linux nodes only

## Context

ADR 0008's 2026-06 amendment made macOS a supported production platform by
running the Linux-ELF OpenShell gateway inside a Linux container on Docker
Desktop's LinuxKit VM. The hub itself (a darwin/arm64 host running the control
plane as a launchd LaunchDaemon) runs that way, so this is not a hypothetical
platform. Two costs have since become clear.

**The architectural cost, which the amendment already admitted.** ADR 0008
line 93 states it plainly:

> Landlock is waived on macOS. Docker Desktop's LinuxKit kernel does not
> surface `/sys/kernel/security/lsm` to containers, so the operator policy's
> `landlock: best_effort` filesystem confinement is not enforced.

So the posture was: run an entire Linux VM on macOS in order to run a Linux
sandbox, and then lose the filesystem confinement that was a principal reason
to choose that sandbox. seccomp, namespaces and the L7 egress proxy still
enforced; path confinement did not. macOS nodes carried
`MAC_OPENSHELL_ALLOW_NO_LANDLOCK=1` as a documented waiver.

**The operational cost, observed 2026-08-16/17.** The hub could not take *any*
source update, and the only symptom was a rollback event nobody was reading:

```
worker.agentbus.repo_update.rolled_back
  before_sha 68602744 -> after_sha 68602744  attempted_after_sha 44820d4a
  openshell_image_rebuild.status = managed_image_stale
  summary "source update required a new published runtime digest; checkout rolled back"
```

The chain: `worker.py`'s `_resolve_openshell_docker_bin()` carefully works
around "macOS launchd jobs do not inherit the interactive shell PATH" and
resolves `/Applications/Docker.app/.../docker` correctly — but `docker` then
invokes its credential helper *by bare name* through `$PATH`, because
`~/.docker/config.json` sets `credsStore=desktop`. The helper lives in that
same Docker.app directory, which the launchd PATH does not contain:

```
error getting credentials - err: exec: "docker-credential-desktop":
executable file not found in $PATH
```

The image pull failed, `managed_image_stale` fired, and the checkout rolled
back. The workaround for the macOS PATH problem was incomplete in exactly the
way the macOS/Docker integration invites, and the blast radius was a frozen
hub.

The operator's decision, stated 2026-08-17:

> I no longer wish to enforce docker for either macos or windows — if I am
> running these instances they are either already virtualized instances or I
> have a very powerful security model which can be set on the hosts or both

> not running macos agents under containers anymore though, got it? strictly
> host installs of everything currently inside the container

and, on what a macOS agent is:

> yes, a completely standard darwin application will work fine on macos

## Decision

**The managed OpenShell runtime is Linux-only. A macOS fleet node is a plain
host install: a standard macOS application, installed on the host, with no
container runtime.**

macOS nodes attest the isolation posture **`macos_host`**.

The name is deliberately literal. The retired posture string was
`macos_docker_vm_seccomp_egress`, which named four protections; of those, the
Landlock waiver already conceded one, and this decision removes the other
three. `macos_host` names no protection because the host install provides
none beyond what macOS gives any ordinary application. A posture string is an
attestation that other code trusts to make routing decisions. An honest weak
posture is strictly better than a flattering false one, and a posture must
never be named after a protection it does not deliver.

**Linux is unchanged.** Linux nodes keep `landlock_enforced`, the managed
runtime image, the OpenShell policy, kernel-enforced Landlock, seccomp,
namespaces and the deny-by-default egress proxy. Containerized execution
remains the model there, and the attestation validator still requires the full
digest-bound container tuple on Linux. This decision must not be read as
weakening Linux; a Linux node may not adopt `macos_host`, and a darwin node may
not adopt `landlock_enforced`.

## What is and is not enforced on a darwin node

Written out, because a reader in a year needs the delta without interpretation.

| Property | Linux node (unchanged) | macOS node (before) | macOS node (now) |
| --- | --- | --- | --- |
| Filesystem path confinement | Landlock, kernel-enforced | **waived** | none |
| Syscall filtering (seccomp) | enforced | enforced (LinuxKit VM) | **none** |
| Namespace isolation | enforced | enforced | **none** |
| Deny-by-default L7 egress proxy | enforced | enforced | **none** |
| Per-task throwaway sandbox identity | yes | yes | **no — tasks run in the host user's account** |
| Immutable pinned runtime image | yes | yes | **no — host toolchain** |
| Executor / Python / script / source digest binding | yes | yes | **yes, unchanged** |

What remains on a macOS node is the *integrity* half of the attestation, not
the *confinement* half: the executor binary, its Python, its script and the
MAC source bundle are still SHA-256 digest-bound and still revalidated against
the hub-approved tuple before a repository-bearing report runs. What is gone is
every runtime boundary between the agent and the machine. A task on a macOS
node can read and write anything the agent's user account can, and can reach
any network the host can reach.

The operator has accepted this explicitly, on the stated basis that such hosts
are either already virtualized or carry host-level controls, or both. That is a
deliberate trade, not an oversight — but it is a real reduction in enforced
isolation and is recorded here as one.

## Implementation

- `models.py` — the `(platform, isolation_posture)` allowlist is now
  `{("linux", "landlock_enforced"), ("darwin", "macos_host")}`. On a host
  install the four container-only fields (`runtime_image_ref`,
  `policy_sha256`, `openshell_bin_path`, `openshell_bin_sha256`) must be
  **empty**. An empty field is an honest "not applicable"; a fabricated digest
  would be an attestation that lies. A darwin attestation that also claims an
  image, a policy or an OpenShell binary is rejected.
- `worker.py` — darwin builds the attestation without requiring
  `MAC_OPENSHELL_SANDBOX`, an OpenShell binary, a policy or a runtime image
  ref. Environment hygiene checks (no `PATH` passthrough, no retained
  sandboxes, allowlisted passthrough only) remain enforced on every platform.
  `_managed_openshell_source_update_guard` and
  `_maybe_rebuild_openshell_image_after_update` no-op off Linux, so a stale
  marker from the Docker era can no longer roll back every source update on the
  node — the failure that froze the hub.
- `executor_sandbox.py` — `_assert_approved_read_only_report_runtime` accepts
  the host-install shape and refuses a host install that claims a container.
  `_ensure_landlock_or_fail` no longer treats darwin as a waiver case: the
  managed sandbox is Linux-only, so `MAC_OPENSHELL_SANDBOX` set on darwin is a
  misconfiguration, not a posture.
- `services.py` / `fleet_release_epoch_service.py` — the
  `openshell_required` precondition for minting the report-executor marker is
  satisfied by a host-install attestation. There is nothing on darwin for that
  flag to assert.
- `deploy/openshell/bootstrap-openshell.sh` — the entire `bootstrap_darwin()`
  Docker-Desktop path is removed. macOS exits successfully with an explanation;
  a macOS node with no OpenShell is correctly provisioned, not broken.
- `deploy/fleet-node-install.sh` — darwin always takes the "optional OpenShell
  runtime disabled" path, records `MAC_OPENSHELL_SANDBOX=0`,
  `MAC_OPENSHELL_REQUIRED=0`, `MAC_ALLOW_UNSANDBOXED_YOLO=1` in `mac.env`, and
  drops `MAC_OPENSHELL_ALLOW_NO_LANDLOCK`. Legacy gateway-container cleanup no
  longer *requires* Docker: if Docker is absent or unreachable, no container
  can be running, and its absence must not block the migration that removes the
  dependency on it.

`MAC_OPENSHELL_ALLOW_NO_LANDLOCK` survives as a Linux-only audited override.
It is no longer the macOS posture, because macOS no longer has one to waive.

## Consequences

- The hub can take source updates on darwin again, with no Docker installed and
  no managed-image markers present.
- macOS nodes run tasks unconfined by MAC. Anything that must be confined must
  run on a Linux node.
- The host install must now supply, natively, everything the runtime image
  supplied. It does not yet: Node/npm/pnpm, the reviewed coding-agent CLIs
  (`claude`, `codex`, `cursor-agent`), the `[dev]` extra needed to run contract
  tests in place, cmake/ninja/llvm-objcopy/ld.lld/qemu-system-riscv64, java and
  lein have no darwin install path, and `deploy/verify-bash-contract.sh`'s
  `$BASH == /bin/bash` assertion is unsatisfiable on macOS (its `/bin/bash` is
  3.2). Several host-side code paths also hardcode `/opt/mac-venv/bin` and omit
  `/opt/homebrew/bin`. These are tracked separately; until they are closed, a
  macOS node is a *steward* host, not a general execution host.
- Windows is out of scope here. The ledger task that motivated this work names
  it, but the operator's decision as stated concerns macOS, and WSL2 already
  presents as Linux. Nothing in this ADR describes Windows.
