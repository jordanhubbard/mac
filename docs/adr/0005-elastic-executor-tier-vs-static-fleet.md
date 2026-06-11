# ADR 0005 — Elastic executor tier vs. the static fleet (and "every agent is a GitHub runner")

- Status: **Proposed**
- Date: 2026-06-11
- Decision owner: Jordan Hubbard
- Context: the fleet is configured statically in `~/.mac/fleets.yaml` — named
  agents pinned to hosts (rocky→do-host1, natasha→sparky, bullwinkle→puck,
  madmax→madmax), each deployed by `deploy/deploy-mac-fleet.sh` as a long-lived
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
- **Heterogeneous, partly long-lived hardware.** madmax (RTX 6000 Ada) serves
  vLLM as the fleet's LLM brain; bullwinkle (M4 Pro) does local image-gen; the
  in-mac router/media-routing fan work to these. These are **services**, not jobs.
- **The static model's cost, observed directly.** This is where our recurring
  pain comes from: manual SSH deploys; bearer-token **drift** (a 403 that needed
  out-of-band recovery); a default-fleet knob we had to add; ~20 stale
  **beads-bridge** tasks (a static-bridge artifact) making up half the failure
  rate; and c26 repo tasks failing on **dirty/stale checkouts** of
  `/home/jkh/.mac/src/c26`.
- **The repo already leans dynamic.** `docs/k8s-native-rewrite-plan.md`,
  `docs/job-per-task-roles-spec.md`, the `mac autopilot` k8s wiring, and the
  `jordanh-gke` fleet are all "task → ephemeral pod." So we are *already* moving
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

- **It already exists here** (autopilot, `docs/job-per-task-roles-spec.md`,
  `jordanh-gke`) — extending it is the lowest-friction path to elasticity;
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
cluster being off the table; it isn't (`jordanh-gke` is already a cluster), so
GitHub-hosted runners' "no cluster to operate" advantage does not apply.

## Evidence: a live multi-node dispatch experiment (2026-06-11)

The flexible-dispatch claim was **tested, not just asserted**. A real 4-node
Kubernetes cluster was stood up locally (`kind` on `colima`): one control-plane
node (the "hub") + three worker nodes labeled to mimic the fleet's heterogeneity
— `madmax` (`capability-gpu`), `bullwinkle` (`capability-image-gen`), `rocky`
(`capability-ops`). Four Jobs were submitted with no manual placement:

- `nodeSelector: capability-gpu` scheduled onto the `madmax` node;
  `capability-image-gen` → `bullwinkle`; `capability-ops` → `rocky`. Every
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

- **macOS hosts cannot be Linux-container worker nodes.** bullwinkle (darwin /
  M4 Pro) has no kubelet running Linux pods, and its **Metal/MPS GPU is not a
  schedulable k8s resource** at all.
- **NVIDIA GPUs need device plugins.** madmax (RTX 6000 Ada) and natasha (GB10)
  require drivers + the device plugin to expose GPUs as schedulable resources.
- **Cross-network CNI.** Fleet hosts sit on different networks joined by
  Tailscale; one cluster needs node + pod networking across them (e.g. k3s + an
  overlay/tailnet CNI) — feasible, not "simple."
- **MAC semantics are still ours to build.** k8s supplies the substrate; the
  `claim → run → evidence → exit` executor and autoscaling off `GET /tasks/ready`
  are not provided by k8s.

Crucially, the hosts that *can't* be worker nodes (darwin/image-gen, GPU model
servers) are exactly the ones this ADR assigns to the **persistent tier**. The
experiment therefore confirms both the dispatch property *and* the two-tier
split: Linux hosts → elastic executor nodes; darwin + GPU + long-lived-service
hosts → persistent agents.

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
