# ADR 0012 - Native node steward with containerized task execution

- Status: **Accepted; implementation deferred pending fleet measurement**
- Date: 2026-07-22
- Decision owner: MAC fleet owner
- Baseline commit: `5b5883adb595c9ac962a26c7e327f7f522fb94ae`

## Context

MAC operates across heterogeneous machines rather than one uniform server
class. The current fleet includes a macOS host managed by launchd, Linux hosts
managed by systemd, and Kubernetes pods managed by supervisord. CPU
architectures, available hardware, network routes, privilege boundaries, and
container-runtime ownership also differ between nodes.

Two deployment models are already present:

1. The persistent MAC control and agent processes installed by fleet deploy run
   directly under the node's native supervisor and use `~/.mac/venv` for the
   main Python runtime.
2. Coding and review work is executed in OpenShell sandboxes built from an
   immutable OCI runtime image. Python in that image is installed under
   `/opt/mac-venv`.

Making every process run in a container initially appears to provide a single
system image. It would improve dependency reproducibility and eliminate some
host package drift, but it would not make the underlying machines homogeneous.
The containerized process would still need host-specific access to Docker,
launchd or systemd, GPUs, filesystems, credentials, mesh networking, and
recovery controls. In particular, giving an ordinary agent a mounted Docker
socket would give it effective control of the host while adding a second
failure boundary.

Recent fleet failures also have different causes. Immutable images can prevent
client/runtime version skew and can provide native `linux/amd64` and
`linux/arm64` artifacts. They do not repair supervisor-cohort coupling, DNS,
SSH routing, credential delivery, Tailscale state, or a failed container
daemon. A universal-container rule would therefore solve only part of the
observed problem while making host recovery depend on more infrastructure.

## Amendment (2026-08-17): containerized execution is Linux-only

[ADR 0015](0015-macos-nodes-are-host-installs.md) confines the containerized
half of this hybrid to Linux nodes. The native-steward half is unchanged and
now covers the whole of a macOS node: the steward *and* task execution run
natively there, under the `macos_host` isolation posture, because macOS has no
container runtime in the fleet any more. Read every "containerized execution"
statement below as "on Linux nodes".

## Decision

MAC adopts a **hybrid native-steward/containerized-execution architecture**.

The durable invariant is:

> Every task execution environment is immutable, architecture-matched,
> digest-attested, and isolated. Only the minimal host-integration steward may
> run natively.

This replaces the proposed blanket invariant that every agent process must run
in a container.

### Native node steward

Each fleet node may run a small native steward under its platform supervisor.
The steward owns only operations that inherently cross the host boundary:

- node identity, registration, heartbeat, and capability reporting;
- launchd, systemd, supervisord, or Kubernetes lifecycle integration;
- container-runtime availability and image activation;
- hardware and architecture discovery;
- explicit workspace, device, and secret-mount preparation;
- fix-forward health reporting and recovery of the active generation.

The steward must not become a general project runtime. It must not install
repository toolchains, run agent-authored build commands, or accumulate the
language dependencies of fleet projects. Its dependency surface must remain
small enough to diagnose and repair when the task runtime is broken.

An implementation may retain a small standard-library recovery helper outside
the task virtual environment, or replace it with a static binary. Such a helper
is part of the steward boundary, not a task executor.

### Containerized execution plane

Coding agents, reviewers, integration workers, repository commands, and other
dependency-heavy or agent-directed workloads run in OpenShell-managed
containers. Ordinary task containers must not receive the host Docker socket
or unrestricted supervisor control.

Python task images use a dedicated in-image environment, conventionally
`/opt/mac-venv`. The virtual environment provides a deterministic dependency
path; the container and OpenShell policy provide the security boundary. A
Python virtual environment alone is not treated as isolation.

Runtime images must be:

- published by immutable digest;
- qualified against a recorded runtime-input identity;
- built for each admitted production architecture, initially `linux/amd64` and
  `linux/arm64`;
- smoke-tested on the architecture on which they will run;
- activated through the existing fix-forward generation protocol.

Architecture emulation is not an acceptable silent fallback for production
workers. A node whose native image is absent remains unavailable for task work
and reports the missing platform explicitly.

### Supervisor and platform boundaries

The fleet continues to use supervisor-homogeneous rollout lanes. launchd,
systemd, and supervisord/Kubernetes remain platform adapters for the same
logical deployment contract; they are not required to have identical
implementation details or failure modes.

Containerization does not permit a failing platform lane to block an already
proved independent lane. All lanes converge on the same reviewed source and
runtime identities, but readiness and fix-forward recovery are evaluated per
lane.

## Deployment hold and measurement phase

This ADR records architectural direction only. **It does not activate a fleet
topology migration and must not block the rollout in progress on 2026-07-22.**

Specifically, accepting this ADR:

- adds no new deploy precondition;
- adds no containerization conformance gate;
- changes no service unit, plist, supervisord program, image, or fleet record;
- does not drain, restart, or disqualify an existing agent;
- does not classify the current host-supervised agents as rollout failures.

Phase 0 is to return the current fleet to service and collect an operational
baseline without a significant topology perturbation. The baseline must retain
per-agent and per-platform dimensions and include:

- availability and restart/recovery frequency;
- claim latency, start latency, and end-to-end task duration;
- task completion, review acceptance, rework, and substantive-publication
  rates;
- sandbox startup and image-pull time;
- failures classified as source/runtime drift, architecture, supervisor,
  container runtime, network, credentials, repository, tests, or model work;
- operator intervention and time to recovery;
- resource use sufficient to identify container or VM overhead.

No fixed calendar duration is encoded here. The fleet owner will decide when
the sample contains enough meaningful work across the heterogeneous lanes.
Implementation requires a separate reviewed change that cites this baseline
and defines its canary and reversal criteria.

## Migration constraints

If the measured evidence supports further containerization, migration proceeds
incrementally:

1. Preserve the native steward boundary and current task-container contract.
2. Prove multi-architecture image publication and attestation independently of
   service relocation.
3. Canary any thinner steward or containerized persistent worker on one Linux
   node before expanding its supervisor lane.
4. Evaluate macOS separately because Docker runs through a Linux VM and cannot
   replace native launchd and host-integration responsibilities.
5. Compare the canary against the Phase-0 baseline. Promote only when it
   improves reproducibility or recovery without degrading useful task output,
   availability, or operator burden.

There is no big-bang fleet conversion. A later implementation that cannot
preserve per-lane fix-forward recovery or requires ordinary task containers to
control the host container daemon must return for a new architectural decision.

## Alternatives considered

### Containerize every agent and service

Rejected as a fleet-wide invariant. It provides a uniform packaging surface
but makes native host integration indirect, creates nested-runtime problems on
Kubernetes, introduces a Linux VM dependency on macOS, and offers little
security benefit to a process holding the Docker socket.

### Keep every agent host-native

Rejected for the execution plane. Host virtual environments do not adequately
bound agent-authored commands or prevent repository toolchains from polluting
one another. This also gives up the immutable image and architecture
attestation benefits already provided by OpenShell.

### Hybrid steward and execution plane

Accepted. It puts the stable platform adapter at the host boundary and the
variable, dependency-heavy, agent-directed work inside the strongest existing
isolation and provenance boundary.

## Consequences

- Current fleet recovery can continue without waiting for an architecture
  migration.
- Task environments retain one reproducible OCI and OpenShell contract across
  host types.
- Native platform differences remain visible and testable instead of being
  hidden behind an incomplete single-system-image abstraction.
- The native steward becomes a deliberately constrained trusted component and
  needs its own small compatibility matrix.
- Image publication must remain multi-architecture and provenance-aware.
- A future topology change is evidence-gated by meaningful fleet work rather
  than justified only by packaging uniformity.

## Relationship to prior decisions

- ADR 0005 separates elastic executors from the static fleet; this ADR defines
  the host boundary for those executors.
- ADR 0008 keeps Docker Engine/Moby as OpenShell's container runtime; this ADR
  does not broaden Docker socket access.
- ADR 0009 keeps task images minimal and provisions project-specific layers on
  demand; this ADR preserves that separation rather than moving project
  dependencies into the native steward.
