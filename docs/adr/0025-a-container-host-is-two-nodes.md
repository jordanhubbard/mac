# ADR 0025: A host running a container environment is two nodes

- Status: Proposed
- Date: 2026-08-20
- Decision owner: MAC fleet owner
- Follows: [ADR 0015](0015-macos-nodes-are-host-installs.md) — macOS nodes are
  host installs, not containers
- Amends: [ADR 0012](0012-hybrid-native-steward-containerized-execution.md) —
  the containerized half is a property of a *node*, not of a *machine*

## Context

A deploy phase cannot begin until the node proves quiescence, and part of that
proof is that no managed containers are running. The gate discovers every
docker/podman binary on the host and requires `info` to return 0 on each. An
installed-but-uninspectable daemon is treated as unknown state, never as an
absence proof — sound reasoning, because a stopped daemon can still own
restart-managed containers that reappear later.

The reasoning is sound and the premise is wrong. **A node cannot prove the
absence of something it was never supposed to have.**

### This has now blocked the fleet three times

- **2026-08-05** — a vestigial podman on the hub returned exit 125 while docker
  was the real runtime. The hub became undeployable. Remedy: `podman machine
  start`, and the machine was **left running permanently** — a 2GiB VM with
  zero containers, alive only to satisfy a probe (`task_0b164136`).
- **2026-08-17** — ADR 0015 removed containers from macOS nodes entirely:
  "strictly host installs of everything currently inside the container".
- **2026-08-20** — the same probe failed the hub's own deploy again:

      daemon-resource quiescence failed: container runtime is unreadable:
      podman .../podman-remote via podman-machine://podman-machine-default@...
      (exit 125): OS: darwin/arm64

  The message names the platform it should have exempted. Two of three cohort
  members had already passed; the fleet was left undeployable by a container
  runtime that ADR 0015 says the node does not use.

Each time the remedy has been to keep a virtual machine alive rather than
answer the question.

### Why "just skip the probe on darwin" is wrong

It was tried while writing this ADR and withdrawn. Two objections, both real:

1. ADR 0015 **created a migration**. macOS hosts that previously ran the
   containerized gateway may still hold legacy containers, and the moment after
   retiring that path is the worst time to stop looking for its leftovers.
2. `test_macos_podman_machine_connection_is_explicitly_certified` asserts the
   opposite, deliberately. A test that disagrees is a design disagreeing, not
   collateral damage.

The operator declaration added for `task_0b164136`
(`MAC_DEPLOY_CONTAINER_RUNTIME_PATHS`) does not help either, and correctly so:
`test_an_empty_declaration_is_not_a_declaration` refuses to let an operator
declare "there are none", because that would "certify absence on a host nobody
actually inspected."

So every available answer is closed, which is the signal that the model is
wrong rather than the code.

### The model that is wrong

The gate assumes one machine has one container posture. It does not.

A fleet node may be macOS, Linux, Windows or BSD. On three of those, **having no
container runtime is a legitimate steady state, not a fault and not a
transient**. There is no probe that distinguishes "this platform never has
containers" from "this platform has containers and they are hiding", because
the first has no positive evidence to offer. Absence is not observable.

Worse, the probe finds things that are not the node. Docker Desktop on macOS is
a **Linux virtual machine**. Probing it from the macOS node conflates two
machines with different kernels, different filesystems, different lifecycles and
different owners — and then reports the VM's health as the Mac's readiness. That
is what happened on 2026-08-20: a developer's homebrew podman, unrelated to the
fleet, decided whether the hub could deploy.

## Decision

**A machine that runs a Linux container environment is modelled as two nodes.**

- The **native node** — macOS, Windows or BSD — attests `macos_host` (or its
  platform equivalent). It has no container posture to prove, because it has no
  container path. Container quiescence does not apply to it and is not run
  against it.
- The **container node** — the Linux environment inside Docker Desktop, a
  podman machine, or a VM — is a distinct node with a Linux posture, its own
  identity, its own lifecycle, and its own quiescence obligations. Container
  quiescence applies to it, in full, unchanged.

The two share hardware and nothing else that matters to a deploy. They are
already two operating systems, two kernels and two filesystems; the registry
should say so.

### What follows

1. **Container absence is never inferred from a probe.** It follows from the
   node's declared posture. A node with no container posture is quiescent with
   respect to containers by construction, and the gate does not ask.
2. **A container runtime found on a native node is not that node's business.**
   It is either a separate registered node, or an unrelated developer tool. It
   must never gate the native node's deploy, and the gate must not read its
   health as the native node's readiness.
3. **The Linux node keeps every existing guarantee.** An
   installed-but-uninspectable daemon there is still unknown state, never an
   absence proof. Nothing in this ADR softens the Linux gate; it narrows what
   the gate is asked about.
4. **Legacy containers get a migration path, not an exemption.** A macOS host
   that previously ran the containerized gateway has its container environment
   registered as a Linux node and drained there — the concern that blocked the
   simple darwin skip is answered by modelling, not by ignoring it.

## Consequences

- The hub stops being undeployable because a developer's podman machine is
  stopped. The 2GiB VM kept alive since 2026-08-05 can be shut down.
- The registry grows nodes that share a machine. `machine` and `node` stop being
  synonyms — an overdue distinction, since they already were not synonyms in
  fact.
- Deploy targeting must be explicit about which of the two it means. A cohort
  naming a machine is ambiguous under this ADR and should name nodes.
- Platform coverage becomes statable. "Windows and BSD nodes have no container
  posture" is a sentence the model can express; today it can only be encoded as
  a probe that happens not to find anything, which is indistinguishable from a
  probe that failed.
- An attestation gains meaning. `macos_host` currently describes how the node
  was installed; under this ADR it also says what the node is not, and the gate
  can rely on that.

## Alternatives considered

**Skip container quiescence on darwin.** Rejected above: it blinds the gate to
legacy containers during the migration that ADR 0015 created, and contradicts a
deliberate contract test.

**Let the operator declare "no runtimes".** Rejected, and already rejected in
code: it certifies absence on a host nobody inspected. The declaration
mechanism is for saying *which* runtimes are real, not for asserting a negative.

**Treat a stopped machine as absence.** Rejected on measurement. On the hub,
2026-08-07, a podman machine that was running at that moment reported
`LastUp 0001-01-01T00:00:00Z` — the applehv provider never maintains the field,
so "never up", "up right now" and "stopped, holding containers" are
indistinguishable. This would manufacture a false proof on a live runtime.

**Keep the VM running forever.** The status quo since 2026-08-05. It is a
workaround that has now failed twice, costs a permanently running virtual
machine, and encodes the wrong model: it makes the Mac pretend to be a
container host so a probe designed for container hosts can succeed.
