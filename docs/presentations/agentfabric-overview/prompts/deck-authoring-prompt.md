# Deck authoring prompt — AgentFabric overview

Use this when asking a model to draft or revise slide content for this package.
It encodes the constraints the specifications impose, so that a draft arrives in
a form `build_deck.py` can implement without re-litigating the ground rules.

## The brief

You are writing for an audience that is highly technical and completely
unfamiliar with both AgentFabric and the requirements of multi-agent,
multi-model orchestration at fleet scale. Assume they can read a state machine
and a trust boundary; assume they have never heard of this system, and do not
assume they accept that orchestration needs a control plane at all — earn that.

Order is marketing first, then mechanism:

1. What the system does, in outcomes a reader can check.
2. Why chat-only orchestration does not scale to a fleet.
3. Where AgentFabric leverages NVIDIA technology, and where it leverages open
   source — with the boundary each one owns.
4. What AgentFabric adds that none of those provide.
5. Who the actors are and which single authority each one holds.
6. How one task lives, how agents coordinate, how the fleet and the models are
   modelled, and what evidence is left behind.
7. What is implemented, what is decided but not yet running, and what is
   proposed.

## Form constraints

- Prefer a diagram or a flow to a bullet list. A slide that could be a flow and
  is instead six bullets is a rejected draft.
- Bullets are allowed for one purpose: naming high-profile NVIDIA or OSS
  projects AgentFabric is built on. Name the project and the boundary it owns.
- Every diagram must be expressible in native shapes: rectangles, rounded
  rectangles, chevrons, chips, connectors, arrows, and text frames. If a visual
  needs an illustration to work, it is the wrong visual.
- Each slide carries: a claim as its title, one visual that proves it, and
  speaker notes that give the mechanism plus any qualification.
- Titles are sentences that assert something. "Coordination is a town square,
  not a switchboard" is a title; "Coordination" is not.

## Factual constraints

- Every claim must exist in `source-notes.md` with an authority in the tree. If
  a claim is not there, add it there first or drop it.
- Re-use is stated as re-use: a dependency is named as a dependency, a design
  reference as a reference. NemoClaw is a reference, not the deployed gateway.
  Unbounded elastic capacity is a proposal, not a shipped capability.
- Never blur status. An accepted decision whose implementation is deferred is
  neither shipped nor proposed, and gets its own label.
- No ROI, productivity, throughput, or maturity numbers. Counted figures carry
  the command and date that produced them, and describe surface area only.
- Do not overstate metering: route events record what the caller reported today;
  enforcing metering at the router is a proposal, and the coverage gap is
  published with its measurement window.

## Output shape

Return, per slide: the number, the title claim, the visual as a described
layout, the text that appears inside the visual, and two or three speaker notes.
Do not return prose paragraphs intended to be pasted onto a slide.
