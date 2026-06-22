# ADR 0008 - OpenShell uses Docker Engine/Moby as its only container runtime

- Status: **Accepted**
- Date: 2026-06-20
- Decision owner: `<user>`

## Context

OpenShell can run sandboxes through more than one container driver, and MAC had
started to encode that as host-specific behavior: some fleet nodes used Podman,
others used Docker. That split creates different image stores, gateway configs,
GPU/CDI behavior, service dependencies, and debugging paths. It also caused a
real validation failure: an image that existed in one runtime path was stale or
unusable in another.

The runtime must be the same on bare metal, VMs, and containerized deployments
where nested Docker is supported. It also must avoid Docker Desktop licensing or
desktop-runtime behavior.

## Decision

MAC/OpenShell standardizes on **Docker Engine/Moby** through OpenShell's Docker
driver.

Production fleet nodes must not use Docker Desktop, Podman, or `podman-docker`
for OpenShell execution. The `docker` CLI must point at a real Docker
Engine/Moby daemon. The OpenShell gateway config must advertise only:

```toml
compute_drivers = ["docker"]

[openshell.drivers.docker]
default_image = "localhost/mac-hermes:net"
```

The bootstrap script owns this contract:

- install/start the OSS Docker Engine/Moby daemon on apt-based Linux hosts when
  missing;
- reject `OSH_DRIVER=podman`;
- replace `podman-docker` with Docker Engine/Moby automatically on apt-based
  Linux hosts, and reject any remaining `docker` command that is still Podman
  emulation;
- install the configured `OPENSHELL_VERSION` for both `openshell` and
  `openshell-gateway` on every run, replacing older binaries instead of
  tolerating version drift;
- build `localhost/mac-hermes:net` with the same Docker daemon the gateway uses;
- write only the Docker driver section in `~/.mac/openshell/gateway.toml`;
- smoke-test the image through `openshell sandbox create` before reporting
  success, proving the runtime-visible image contains `gh`, `codex`, and
  `codegraph`.

Compatibility note: OpenShell 0.0.62 can accept the Docker-driver config above
while the gateway logs still show `openshell_driver_podman` and it consults the
user's Podman image store. Until that upstream/runtime mismatch is eliminated,
the bootstrap mirrors the Docker-built `localhost/mac-hermes:net` tag into the
runtime-visible image store and then runs the OpenShell smoke test. This is a
deployment reconciliation step; Docker Engine/Moby remains the authoritative
build path and `podman-docker` is still rejected.

## Consequences

The fleet has one image store, one OpenShell driver path, and one debugging
model. Containerized environments use the same path by providing a nested Docker
daemon or mounted Docker socket. Non-Linux developer machines must validate in a
Linux VM/container with Docker Engine/Moby; Docker Desktop is not a production
dependency.

Podman can remain installed on a host for unrelated work, but it is not part of
MAC/OpenShell runtime selection and must not be exposed as `docker` via
`podman-docker`.

## Amendment (2026-06): macOS fleet nodes via Docker

The original decision treated non-Linux machines as developer-only ("validate
in a Linux VM"). A macOS host (e.g. an Apple-Silicon workstation with large
RAM/cores) is now a supported **production fleet node** — including the hub —
using Docker on macOS, under these constraints:

- **Runtime is still Docker Engine/Moby**, accessed through the macOS Docker
  daemon. Docker Desktop is acceptable for a single-owner macOS node (it ships
  a Moby engine in a LinuxKit VM); `podman-docker` remains rejected. The
  `compute_drivers = ["docker"]` + `[openshell.drivers.docker]` contract is
  unchanged.
- **The gateway (a Linux ELF) runs inside a Linux container** on that Docker
  daemon, with the Docker socket mounted, creating sandbox + supervisor
  *sibling* containers. The gateway's data dir is mounted at an **identical host
  path** (`-e HOME=$DIR -v $DIR:$DIR`) so the supervisor-binary bind mounts it
  emits resolve on the Docker host. The host-side `openshell` CLI (a Python
  package, `uv tool install`) drives it over a published loopback port.
- **Landlock is waived on macOS.** Docker Desktop's LinuxKit kernel does not
  surface `/sys/kernel/security/lsm` to containers, so the operator policy's
  `landlock: best_effort` filesystem confinement is not enforced. seccomp,
  namespaces, and the deny-by-default L7 egress proxy still enforce. macOS nodes
  therefore set `MAC_OPENSHELL_ALLOW_NO_LANDLOCK=1` (an audited, documented
  posture for a single-owner fleet, not a silent fallback). `task_executor`'s
  `_ensure_landlock_or_fail` recognizes darwin and honors this override with a
  macOS-specific message; `deploy/openshell/bootstrap-openshell.sh` has a
  self-contained darwin branch that installs the CLI, builds the image, runs the
  gateway container, renders the policy, and writes this env recipe.

Linux fleet nodes are unchanged (native Landlock, systemd-managed gateway).
