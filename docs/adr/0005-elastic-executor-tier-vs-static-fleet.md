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

## TL;DR verdict

The proposal bundles two ideas; **separate them**:

| Idea | Verdict |
| --- | --- |
| **Elastic / dynamic registration** instead of static `fleets.yaml` | **Yes** — overdue. The static model is the source of most operational pain we keep hitting. |
| **Agent ≡ GitHub runner (universally)** | **Half right.** True for the *executor* tier; **wrong** for the *persona/services* tier. |

A GitHub runner is **stateless, fungible, short-lived**. A MAC agent, as built, is
a **stateful persona** (soul, long-term memory, mood, Slack presence, self-driven
behavior). Forcing every agent into the runner mold throws away the identity
model the whole Hermes layer (ADR 0001) is built on. The right move is a
**two-tier split**: keep a small persistent persona/services tier, and make the
**executor tier elastic** (ephemeral workers that claim a ledger task, run in a
clean checkout, emit evidence, and die) — with the **MAC task ledger remaining
the single dispatch brain**.

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
4. **Projects → runner groups / labels, not "all projects on every runner."**
   Capabilities become labels; per-project isolation becomes runner groups so a
   worker only holds the secrets of the project it serves.

## Substrate: GitHub runners vs. k8s Jobs

Both implement the same elastic-executor idea; they are **competing substrates**,
not different architectures:

- **GitHub ARC / ephemeral runners** — buys GitHub's registration, labels, runner
  groups, and the natural fit for repo/CI-shaped work (clean checkout + the
  `pushed=true / pr_url` evidence contract we already enforce). Cost: self-hosted
  runner **security** (agentic code + repo write + secrets is GitHub's canonical
  footgun) and a tendency to drag GitHub's event scheduler into the picture.
- **k8s Jobs (already partly built here)** — we own the scheduler and the
  isolation; integrates with the existing autopilot/job-per-task work and
  `jordanh-gke`. Cost: we build the labeling/identity ourselves.

Recommendation: prefer **k8s Jobs** as the executor substrate where we already
have it, and treat "register as a GitHub runner" as an *additional* on-ramp for
genuinely repo/CI-shaped work, not the universal model. Keep the executor
contract substrate-agnostic so either can drive it.

## Risks / non-goals

- **Do not** dissolve personas/memory/long-lived services into ephemeral workers
  — that breaks ADR 0001's identity model and the brain/router.
- **Secret blast radius:** a worker serving "all projects" can read all those
  repos' secrets + TokenHub keys. Scope per project (runner groups / scoped
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
