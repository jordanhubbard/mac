# Narrative specification — AgentFabric overview

This file is the authority for the narrative member (`build_narrative.py` →
`agentfabric-overview.docx`). The narrative is prose, not slide fragments: a reader who
has only this document must be able to reconstruct how the system works. It carries the
same claims as the deck at greater depth and must not diverge from it on a shared claim.
Factual claims trace to [source-notes.md](source-notes.md).

## Contract

- Same audience as the deck: highly technical, new to AgentFabric and to fleet-scale
  multi-agent / multi-model orchestration.
- Same order as the deck: what it does, then what it re-uses and why, then actors and
  mechanism.
- Written in complete paragraphs. Lists are permitted only for the NVIDIA and open-source
  project inventories.
- Every mechanism claim names its authority in the control-plane tree (module, document,
  or CLI surface).
- Implemented, decided-but-not-yet-runtime, and proposed are kept distinct in wording, not
  merely in one summary section.
- No invented ROI, productivity, or throughput figures. Counts are surface area and carry
  the command that produced them.

## Structure

Heading levels are meaningful: level 1 is a part, level 2 is a section, level 3 is a
mechanism. No level may be skipped.

1. **AgentFabric: one control plane for a fleet of AI agents** (L1)
   - What problem this solves (L2) — conversation is ephemeral state; a fleet needs
     durable state with ownership, ordering, and receipts.
   - Who this document is for (L2) — and what it deliberately does not claim.
2. **What the system does** (L1)
   - Ask once, and the fabric carries it to production (L2) — the six-stage pipeline.
   - The three consequences (L2) — nothing is lost, nothing is blind, nothing
     self-approves.
   - One request, end to end (L2) — the six boundaries and what is written down at each.
3. **Built on the stack, not instead of it** (L1)
   - Why re-use is the design position (L2).
   - Where AgentFabric leverages NVIDIA technology (L2) — OpenShell (L3), NeMo Relay (L3),
     HGX (L3), NemoClaw as reference rather than deployment (L3).
   - Where AgentFabric leverages open source (L2) — state, service, execution, protocol
     (one L3 each).
   - What AgentFabric adds anyway (L2) — durable ledger, named gates, route ladder,
     evidence closure, break-glass (one L3 each).
   - The trust boundary (L2) — what runs inside the sandbox (L3), what never crosses (L3).
4. **Actors, roles, and who is allowed to decide** (L1)
   - The cast and its single-authority rule (L2), one L3 per actor.
   - The life of one task (L2) — the state machine (L3), the four gates (L3), holds versus
     states (L3), recovery of stranded and stalled work (L3).
   - Coordination without a switchboard (L2) — broadcast for intent, ledger for
     consequence (L3), operational learning as secret-free memory (L3).
   - The fleet, modelled honestly (L2) — one L3 per node class.
   - Models and money (L2) — the ordered route ladder (L3), metering at the router (L3),
     pricing at read time (L3).
   - Evidence (L2) — what is recorded per task, per step, and per release (L3); why an
     index is not an authorization (L3).
5. **Scope, honestly stated** (L1)
   - Measured surface area (L2) — counts with their commands.
   - Implemented (L2) / Decided, not yet runtime (L2) / Proposed (L2).
6. **How to adopt it** (L1)
   - Start with one project and one real workload (L2).
   - Grow node classes and coding routes as work demands (L2).
   - Measure before adding capacity (L2).

## Prohibitions

- Do not restate the deck's caption lines as the narrative's argument; the narrative owes
  the reader the mechanism, not the slogan.
- Do not describe the AgentBus as durable state, or evidence as release authority.
- Do not promote a decided-but-unimplemented item into the implemented sections.
