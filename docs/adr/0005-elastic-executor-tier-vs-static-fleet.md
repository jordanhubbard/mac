# ADR 0005 — Elastic executor tier vs. the static fleet (and "every agent is a GitHub runner")

- Status: **Proposed**
- Date: 2026-06-11
- Decision owner: `<user>`
- Context: the fleet is configured statically in `~/.mac/fleets.yaml` — named
  agents pinned to hosts (hub-node, gpu-node, gpu-node-arm, mac-node each on
  their own host), each deployed by `deploy/deploy-mac-fleet.sh` as a long-lived
  editable install + systemd/launchd services. The question raised: *what if
  every agent were a GitHub self-hosted runner (and any runner an agent), with
  agents registered/created dynamically rather than declared statically?*

## Recommendation (the short version)

**Do not make agents GitHub runners.** Get the elasticity you want by adding an
**ephemeral executor tier on k8s Jobs** — which this repo already has (autopilot
/ `job-per-task`) — autoscaled off the MAC ledger's ready-queue, while keeping
personas + shared services persistent. The MAC ledger stays the single dispatch
brain.

GitHub self-hosted runners are the **wrong substrate** for this fleet: they add a
second control plane and identity/label registry that *duplicate* the ledger and
its capability matching; they only fit the repo/CI subset of the work (the rest
gets forced through awkward `repository_dispatch` RPC); and they put agentic code
next to repo-write + secrets (GitHub's canonical self-hosted-runner footgun).
GitHub's only sensible role is the **inverse** of the proposal — a GitHub CI
event *creates a MAC task* (GitHub feeds the ledger; it does not replace it).

Separating the two ideas in the question:

| Idea | Verdict |
| --- | --- |
| Move off the static `fleets.yaml` to **dynamic/elastic** capacity | **Yes** — overdue; it's behind most of our operational pain. |
| **Agent ≡ GitHub runner** | **No.** The executor tier *should* be stateless and elastic, but **k8s Jobs** are the right substrate here, not GitHub runners; and persona/services agents must stay **persistent** (soul/memory/Slack identity, the vLLM brain). |

A GitHub runner is stateless, fungible, short-lived; a MAC agent (as built) is a
stateful persona, and that model (ADR 0001) is worth keeping. The fix is a
**two-tier split** — persistent personas/services + an elastic ephemeral-executor
tier on k8s — **not** turning every agent into a runner.

## What is actually true today (ground truth)

- **Static fleet.** `~/.mac/fleets.yaml` enumerates agents→hosts with
  capabilities + `hub_agent`; `deploy/deploy-mac-fleet.sh` SSHes to each host,
  installs `~/.mac/src/mac` (`pip install -e`) and runs services (control plane
  `mac.service` on the hub; worker loop + Hermes gateway per agent).
- **Agents are durable personas.** Per-agent soul/memory (Qdrant + holographic
  tiers), mood, journal, Slack home channel, ongoing presence, banter,
  service-role election. This is the opposite of a disposable job.
- **Dispatch is a pull/lease model with rich semantics.** The hub ledger
  (`mac.db`) holds tasks; agents claim/lease by `required_capabilities` +
  dependency satisfaction; states include `needs_review/reviewing`; outcomes are
  gated by **evidence/verification contracts** (`verification_contract_failed`,
  "repo evidence requires pushed=true / pr_url"), multi-attempt with
  `deployment_learning`s, and hardware gating (GPU). GitHub's job scheduler has
  almost none of this.
- **Heterogeneous, partly long-lived hardware.** gpu-node (RTX 6000 Ada) serves
  vLLM as the fleet's LLM brain; mac-node (M4 Pro) does local image-gen; the
  in-mac router/media-routing fan work to these. These are **services**, not jobs.
- **The static model's cost, observed directly.** This is where our recurring
  pain comes from: manual SSH deploys; bearer-token **drift** (a 403 that needed
  out-of-band recovery); a default-fleet knob we had to add; ~20 stale
  **beads-bridge** tasks (a static-bridge artifact) making up half the failure
  rate; and c26 repo tasks failing on **dirty/stale checkouts** of
  `/home/<user>/.mac/src/c26`.
- **The repo already leans dynamic.** `docs/archive/field-notes/k8s-native-rewrite-plan.md`,
  `docs/archive/field-notes/job-per-task-roles-spec.md`, the `mac autopilot` k8s wiring, and the
  `<user>-gke` fleet are all "task → ephemeral pod." So we are *already* moving
  off static; the open question is the substrate, not the direction.

## The fulcrum

GitHub runners are designed to be identical, disposable, and stateless; MAC
agents are durable identities that evolve. "Any runner can be an agent" works
only where the work is itself stateless and job-shaped. The mismatch — not the
elasticity — is what makes the universal equivalence wrong.

## Decision

1. **Two tiers, explicitly.**
   - **Persistent tier (few):** personas + shared services (LLM router/brain,
     image-gen, Slack-present teammates). Long-lived (k8s Deployments or a small
     static set). Owns identity and memory.
   - **Elastic executor tier (many):** stateless workers that **claim a MAC task
     → run it in a clean, isolated checkout → emit evidence → exit**. These are
     the dynamically-registered "runners."
2. **The MAC ledger stays the single source of truth and dispatch brain.** It
   keeps the semantics GitHub lacks (deps, review, evidence contracts,
   capability + hardware gating). The runner substrate is a dumb elastic
   *where-to-run*, never a second scheduler.
3. **Autoscale the executor tier off the ready-queue.** The autoscaler polls the
   hub's `GET /tasks/ready` (shipped in `parity-ready-http-01`) — optionally per
   project/capability — and scales the ephemeral worker pool to match. An
   executor's body is essentially `mac task claim → do work → mac task
   evidence/close`.
4. **Per-project isolation, not "all projects on every worker."** Capabilities
   become labels; each project gets its own isolation boundary (k8s namespace +
   scoped service account / `MAC_API_TOKENS`) so a worker only holds the secrets
   of the project it serves.

## Substrate decision: k8s Jobs, not GitHub runners

Use **k8s Jobs** for the executor tier. Concretely, because:

- **It already exists here** (autopilot, `docs/archive/field-notes/job-per-task-roles-spec.md`,
  `<user>-gke`) — extending it is the lowest-friction path to elasticity;
  adopting GitHub ARC means standing up a new runner controller, registration-
  token plumbing, and runner groups.
- **We control isolation** (namespace / RBAC / network policy) — the right
  posture for running agentic code, versus self-hosted runners sharing a host
  with repo-write + secrets.
- **No second scheduler or registry.** The MAC ledger + capabilities already are
  the "runner registry + label matcher," and richer (deps, review, evidence). A
  GitHub runner pool would duplicate that and tempt GitHub's event scheduler into
  becoming a rival dispatcher.

**GitHub self-hosted runners are explicitly not adopted** as the agent or
executor substrate. Their real advantages (registration, labels, runner groups)
just duplicate the ledger; their event/workflow model fits only repo/CI work;
and the security cost is real. GitHub's one legitimate role is an **inbound
bridge**: a GitHub Actions event (push/PR/`workflow_dispatch`) calls the hub to
*create* a MAC task, so external CI can feed the fleet without becoming it.

We keep the executor contract simple (`claim → run → evidence → exit`) so it
*could* run on a GitHub-hosted runner in a pinch — but that is a fallback, not
the design. The only thing that would flip this decision is running a k8s
cluster being off the table; it isn't (`<user>-gke` is already a cluster), so
GitHub-hosted runners' "no cluster to operate" advantage does not apply.

## Evidence: a live multi-node dispatch experiment (2026-06-11)

The flexible-dispatch claim was **tested, not just asserted**. A real 4-node
Kubernetes cluster was stood up locally (`kind` on `colima`): one control-plane
node (the "hub") + three worker nodes labeled to mimic the fleet's heterogeneity
— `gpu-node` (`capability-gpu`), `mac-node` (`capability-image-gen`), `hub-node`
(`capability-ops`). Four Jobs were submitted with no manual placement:

- `nodeSelector: capability-gpu` scheduled onto the `gpu-node` node;
  `capability-image-gen` → `mac-node`; `capability-ops` → `hub-node`. Every
  capability-targeted Job landed on exactly its matching node.
- A 6-completion / parallelism-6 Job with **no** selector spread **2 / 2 / 2**
  across the three worker nodes on the scheduler's own.

**Proven:** a control-plane + worker-node cluster does capability-aware,
self-balancing Job dispatch across the fleet with zero per-host static config —
the elastic-executor property this ADR depends on. (Method caveat: `kind` nodes
are containers on one VM, so the *physical* distribution is simulated; the
*scheduling logic* exercised is genuine, unmodified upstream Kubernetes.)

**What it does NOT prove — and why that sharpens the decision.** The literal
"every host simply becomes a node" does not hold:

- **macOS hosts cannot be Linux-container worker nodes.** mac-node (darwin /
  M4 Pro) has no kubelet running Linux pods, and its **Metal/MPS GPU is not a
  schedulable k8s resource** at all.
- **NVIDIA GPUs need device plugins.** gpu-node (RTX 6000 Ada) and gpu-node-arm (GB10)
  require drivers + the device plugin to expose GPUs as schedulable resources.
- **Cross-network CNI.** Fleet hosts sit on different networks joined by
  Tailscale; one cluster needs node + pod networking across them (e.g. k3s + an
  overlay/tailnet CNI) — feasible, not "simple."
- **MAC semantics are still ours to build.** k8s supplies the substrate; the
  `claim → run → evidence → exit` executor and autoscaling off `GET /tasks/ready`
  are not provided by k8s.

### Why a macOS host can't be a worker node — even with Docker + "GPU pass-through"

Established on an M4 Pro Mac (same class as mac-node):

1. **macOS doesn't run Linux containers; it runs a Linux VM.** Host kernel is
   `Darwin 25.5.0 arm64`, but a container on it reports `Linux 6.8.0 aarch64`
   (Ubuntu): Docker Desktop / colima silently boot a **Linux VM** and run every
   container inside it (Linux images need Linux-kernel namespaces/cgroups that
   XNU/Darwin doesn't implement). A "k8s node on the Mac" is therefore the
   *Linux VM*, not the Mac.
2. **The Apple GPU is on-SoC + Metal-only — there is nothing to pass through.**
   `system_profiler`: *Apple M4 Pro, Bus: Built-In, Metal 4* — reached only via
   the **Metal** API in macOS userspace, not a discrete PCIe card. Linux
   container GPU access (VFIO/IOMMU + the NVIDIA Container Toolkit) forwards a
   *PCI device + its Linux kernel driver* into the container; on Apple Silicon
   there is no PCIe GPU, no Linux driver for it, and Virtualization.framework
   doesn't expose the GPU to the guest. Inside the container `/dev/dri` and
   `/dev/nvidia*` don't exist, and `docker run --gpus all` fails outright
   (`could not select device driver … [[gpu]]`).
3. **The GPU is reachable only by a native macOS process calling Metal** — which
   is by definition not a pod; a Linux Job can never touch Metal. (This is why
   mac-node's SDXL/MPS image-gen runs as a native `~/gen` process.)
4. **Making the Mac a node buys nothing and costs a VM layer:** you'd run Linux
   pods in a VM on the Mac with no access to the one capability that makes it
   special — and the genuinely macOS-only work that needs the host (Metal
   compute; **code-signing / notarization / Xcode builds** — we hit this exact
   wall when the Electron build required the Mac's "Developer ID" / "Fleet
   Identity" signing identities) cannot run in a Linux container at all.

### The Linux/NVIDIA hosts (hub-node, gpu-node, gpu-node-arm) *can* be nodes — container GPU is the supported path

Read-only probes of the actual hosts:

| Host | arch / kernel | GPU (driver) | docker / containerd / nvidia-ctk |
| --- | --- | --- | --- |
| **hub-node** | x86_64 / 6.8 | none (CPU-only) | no / no / no |
| **gpu-node** | x86_64 / 7.0 | RTX 6000 Ada 48 GB (595.71.05) | **yes / yes / yes** |
| **gpu-node-arm** | **aarch64** / 6.17-nvidia | GB10 Grace-Blackwell (580.159.03) | **yes / yes / yes** |

Unlike the Mac, these are architecturally fine as k8s nodes, and the
**container-GPU mechanism that's impossible on macOS is already installed** on
both GPU hosts: driver + NVIDIA Container Toolkit (`nvidia-ctk`) + containerd.
The toolkit is exactly what injects `/dev/nvidia*` + the driver into a container
under `--gpus all`, and the k8s **NVIDIA device plugin** then exposes
`nvidia.com/gpu` as a schedulable resource. (The toolkit's presence is the
dispositive fact — it cannot even exist on macOS, where `--gpus all` errors.)

- **hub-node** — CPU-only Linux; the natural **control-plane ("hub") node** and/or a
  CPU worker. Only gap: **no container runtime today** (it runs the mac services
  as a bare-metal editable install), so containerd must be added.
- **gpu-node** — **GPU worker node: ready.** Add the k8s NVIDIA device plugin and
  pods can request `nvidia.com/gpu`. The often-cited caveat — "one 48 GB GPU is
  held by the long-lived **vLLM brain** (a *Deployment*, not a Job)" — turns out
  to be **hollow**: that brain serves ≈0 traffic (see the routing-evidence
  subsection below), so the GPU can simply be **dedicated to the executor tier**
  rather than carefully shared via time-slicing / MPS. (If a local-LLM fallback
  is wanted, keep a *right-sized* vLLM and GPU-share; just don't let a 0-traffic
  service block the node.)
- **gpu-node-arm** — **GPU worker node: feasible, same mechanism**, with two
  operational caveats: **arm64** (a mixed-arch cluster — images/workloads must be
  built arm64) and a **bleeding-edge driver/kernel** (`6.17-nvidia` + driver 580;
  GB10 drivers are tightly kernel-coupled — the per-kernel-module friction we've
  seen). Architecturally normal; the work is keeping the driver matched to the
  kernel.

Crucially, the **only** host that genuinely *can't* be a worker node is the
**darwin** one (mac-node). The GPU hosts *can* be nodes — but their long-lived
model servers (gpu-node's vLLM brain) are **Deployments**, not Jobs. So the
two-tier split maps cleanly onto the hardware: **hub-node → control-plane / CPU
executor nodes; gpu-node + gpu-node-arm → GPU nodes hosting a persistent model-server
Deployment and/or elastic GPU Jobs; mac-node → off-cluster persistent
native-macOS service.**

### The "vLLM brain" serves ≈0 traffic — the GPU it holds is effectively idle (2026-06-11)

A second probe checked whether the persistent service this ADR is most careful to
protect — gpu-node's vLLM "LLM brain" — is actually load-bearing. It is not.

Querying the live hub's routing telemetry
(`mac --fleet <hub> observability list --name llm.route`), the last ~1000
`llm.route` events (spanning 2026-06-08 → 2026-06-11):

| backend provider | events |
| --- | --- |
| `nvidia` (cloud inference API, resolving `azure/anthropic/claude-sonnet-4-6`) | 994 |
| empty / failed route | 6 |
| **gpu-node / vLLM / its served model** | **0** |

The cause is structural, not transient. gpu-node's vLLM *is* wired into the hub
router — appended to `MAC_ROUTER_PROVIDERS` at priority 0 — but **only for the
model `Qwen/Qwen3.6-27B-FP8`**. Every fleet agent's `gateway_model` (in
`~/.mac/fleets.yaml`, including gpu-node's *own* agent) is
`azure/anthropic/claude-sonnet-4-6`, which routes to the cloud `nvidia` provider.
No caller ever requests the model gpu-node serves, so a 48 GB RTX 6000 Ada sits idle
on the LLM path. (gpu-node was additionally tailscale-offline at probe time.)

**Why this sharpens the decision.** The Decision and Risks sections treat "GPU /
model servers stay persistent services; they are not jobs" as a hard constraint,
and the gpu-node node-readiness note above flagged the vLLM Deployment as the reason
to time-slice rather than dedicate the GPU. The evidence collapses that tension:
the brain has **no traffic to protect**, so gpu-node's GPU is the *easiest*, not the
hardest, to hand to the elastic executor tier — dedicate it outright. The
persistent-services principle still holds in general (it's right for a brain that
*is* serving); it just doesn't currently apply to *this* GPU. A concrete
device-plugin + node-label draft for exactly this conversion lives at
[`deploy/k8s/gpu-worker/`](../../deploy/k8s/gpu-worker/).

(Method/caveat: the telemetry only captures hub-routed requests; a process hitting
`gpu-node:8000` directly would not appear. Confirm with gpu-node's vLLM
`/metrics request_success_total` before decommissioning the service.)

## Risks / non-goals

- **Do not** dissolve personas/memory/long-lived services into ephemeral workers
  — that breaks ADR 0001's identity model and the brain/router.
- **Secret blast radius:** a worker serving "all projects" can read all those
  repos' secrets + TokenHub keys. Scope per project (k8s namespace + scoped
  `MAC_API_TOKENS`).
- **Two-scheduler hazard:** if GitHub events *also* dispatch while the ledger
  dispatches, you get double-dispatch. One brain (the ledger).
- **Cold-start latency:** per-task spin-up is fine for build/edit work, bad for
  conversational turns — another reason persona ≠ ephemeral.
- **GPU / model servers** stay persistent services; they are not jobs.

## Consequences

- **Pros:** elasticity (scale to work, not to a host list); clean ephemeral
  checkouts eliminate the stale-checkout failure class; far less static config
  and manual deploy/token drift; the ledger's rich semantics are preserved.
- **Cons:** a real autoscaler + executor-bootstrap to build and secure; two-tier
  topology to operate; careful secret scoping; reconciliation so the ledger and
  the pool don't fight.

## First step (pilot, reversible)

1. Pick **one project** (e.g. `c26`) whose tasks are repo/CI-shaped.
2. Stand up a small **ephemeral executor pool** (k8s Job-per-task is the shortest
   path given autopilot already exists) labeled for that project's capability.
3. Executor lifecycle: register → `mac task claim` a ready task for the project →
   run in a fresh checkout → `mac task evidence` + `close` → exit. No persona, no
   memory.
4. Drive pool size from `GET /tasks/ready?project=c26`.
5. Compare against the static worker: failure rate (esp. checkout-staleness),
   latency, cost. If it wins, widen to more projects; keep personas/services on
   the persistent tier.

## Open questions

- GitHub runner groups vs. k8s namespaces for per-project isolation.
- How a persistent persona "hands off" a decomposed sub-task to an ephemeral
  executor (ties to `kanban-adopt-01` / parent-child links) and gets results back.
- Whether the persistent tier itself should move to k8s Deployments (retire
  `fleets.yaml` for hosts entirely) or stay a small declared set.
