# Deck specification — AgentFabric overview

This file is the presentation authority. `build_deck.py` realizes it; where the two
disagree, this file is wrong or the builder is wrong, and one of them must change in the
same commit. Factual claims trace to [source-notes.md](source-notes.md).

## Audience and contract

- Highly technical reader, new to AgentFabric **and** new to fleet-scale multi-agent /
  multi-model orchestration. No prior product knowledge is assumed anywhere.
- Marketing before mechanism: what the system does and why it matters comes first; the
  machine comes second.
- Technology re-use is the thesis. Every capability that already exists in NVIDIA or
  open-source projects is named as an integration, not re-described as an invention. The
  mechanisms AgentFabric genuinely adds get their own slide and are not diluted.
- Visual first. A mechanism diagram beats a bullet list. Bullet lists appear only where
  the list itself is the content: the NVIDIA project inventory (7) and the open-source
  project inventory (8).
- Speaker notes on every slide, stating the mechanism behind the claim and the limits of
  the claim. The deck is forwarded without narration.
- No invented ROI, productivity, or throughput numbers. No roadmap item presented as
  shipped.

## Visual system

- 1280 × 720, 20 slides. Ink `#101317`, orange `#F5601A`, panel `#EDF0F2`, fog `#B9C0C8`.
- Native shapes only: chevron flows, round-rect panels, chips, connectors, arrows. No
  raster diagrams and no decorative hero images — the deck must remain editable and
  legible after a Google Slides import.
- Every slide carries a section tag pill, a headline, one supporting sentence, and a
  bottom caption bar that states the takeaway in one line.
- Section dividers (6, 11) are the only text-dominant slides.

## Slides

### Part 0 — what it does (1–5)

| # | Title | Visual | Must say |
| --- | --- | --- | --- |
| 1 | One control plane for a fleet of AI agents. | hub-and-fleet topology | AgentFabric turns a conversation into durable work, dispatches it across a heterogeneous fleet of agents and models, and keeps the receipts. |
| 2 | Ask once. The fabric carries it to production. | ASK → DURABLE TASK → LEASE + DISPATCH → EXECUTE → INDEPENDENT REVIEW → PUBLISH chevron flow, plus three consequence panels | Nothing is lost; nothing is blind; nothing self-approves. The unit of truth is a task in a ledger, not a message in a transcript. |
| 3 | A chat window is not an operating model. | one-agent supervision diagram beside a forty-agent field with no system of record | The failure at fleet scale is structural — lost work, duplicated work, unattributed spend, no answer to "who approved this?" The failure field is illustrative, not a measured failure rate. |
| 4 | Four properties the fabric guarantees. | four panels | Durable truth, honest heterogeneity, evidence by default, spend visibility. |
| 5 | One request, end to end. | six-lane flow (human → gateway → hub → worker → reviewer → publish) with a "what gets written down" row beneath each hand-off | Every arrow crosses a boundary the control plane owns. The gateway owns conversation, personality, and memory and deliberately holds no operational authority. |

### Part 1 — built on the stack (6–10)

| # | Title | Visual | Must say |
| --- | --- | --- | --- |
| 6 | Built on the stack, not instead of it. | section divider | Isolation, observability, inference, containers, scheduling, and coding agents already exist and are maintained by people whose full-time job they are. |
| 7 | Where AgentFabric leverages NVIDIA technology. | control-plane bar over four project columns | OpenShell → execution security (Landlock, seccomp, deny-by-default L7 egress, one guardrail authority). NeMo Relay → optional observability mapping. HGX → bounded, receipt-bearing elastic capacity. NemoClaw → compatibility and design reference for the conversational boundary, not the deployed implementation. |
| 8 | Where AgentFabric leverages open source. | four labelled columns of project bullets | STATE: PostgreSQL, SQLite (local), versioned migrations. SERVICE: FastAPI, Uvicorn, Pydantic, httpx. EXECUTION: Docker Engine / Moby, Kubernetes, OpenClaw, Codex CLI · OpenCode. PROTOCOL: ACP, A2A agent cards, MCP, OCSF event streams. Every box is a dependency, not a fork. |
| 9 | What AgentFabric adds that nothing above provides. | five numbered mechanism cards | Durable ledger, named gates, route ladder, evidence closure, break-glass. Agent output is a candidate; these five are how it earns the right to land. |
| 10 | The trust boundary, drawn explicitly. | inside-the-sandbox panel beside a never-crosses panel | Owned and reaped process tree, allow-listed paths, syscall filter, declared egress, secrets as handles resolved at use. Never crosses: undeclared destinations, raw credential values, another project's repository, self-approval. |

### Part 2 — actors and mechanism (11–19)

| # | Title | Visual | Must say |
| --- | --- | --- | --- |
| 11 | Actors, roles, and who is allowed to decide. | section divider | Every actor holds exactly one kind of authority; no actor holds two that would let it grade its own work. |
| 12 | The cast, and the one authority each actor holds. | eight actor cards | Requester, gateway agent, hub, dispatcher, worker, coding executor, reviewer agent, operator. |
| 13 | The life of one task. | state flow with four numbered gates and a held/terminal chip row | A task is a state machine, not a status string. Gates: dependency, lease, evidence, acceptance. A failed task is a record, not a deletion. |
| 14 | Coordination is a town square, not a switchboard. | broadcast bus with five participants and a lifecycle-verb row | Broadcast for intent and interruption; ledger for consequence. The bus is not the system of record. |
| 15 | A heterogeneous fleet, modelled honestly. | dispatcher bar over four node classes | macOS host install, Linux node with containerized execution, Kubernetes, HGX session. Capability, egress, and secrets are declared per node and per task. |
| 16 | Many models, one ordered route, one meter. | ordered route ladder plus metered/priced panels | The ladder decides who does the work; the meter records what it cost. Metered at the router, priced at read time, attribution gaps reported rather than hidden. |
| 17 | The fleet measures itself. | evidence flow into per-task / per-step / per-release panels | Evidence indexes what happened; it does not by itself authorize a release. |
| 18 | Scale of the implementation. | measured counts | Surface area, not maturity. Every figure is a count from the audited tree, recorded with its command in source-notes.md. |
| 19 | Stated honestly: implemented, decided, proposed. | three-column ledger | The "decided, not yet runtime" column is mandatory. A deck that blurs the three is marketing. |

### Close (20)

| # | Title | Visual | Must say |
| --- | --- | --- | --- |
| 20 | Reuse the stack. Own the truth. | closing statement plus START / GROW / MEASURE adoption steps | One project and one real workload first. The fabric's value appears when a second agent has to trust the first one's output. |

## Prohibitions

- Do not describe NemoClaw as the deployed gateway implementation.
- Do not describe the AgentBus as the system of record.
- Do not present slide 18's counts as maturity, or carry them forward without
  re-measuring.
- Do not move an item from "decided" to "implemented" without an authority in
  source-notes.md.
- Do not add a slide whose only content is a headline over an image.
