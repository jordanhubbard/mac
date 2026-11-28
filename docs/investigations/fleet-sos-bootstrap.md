# Fleet SOS bootstrap minimum

## Question and scope

What is the smallest soul-graph packet from which a cold agent can restart
genuine fleet compounding? This note gives worker-1's candidate, constrained
to at most ten nodes and fifteen edges. It is an evidence-based proposal, not a
canonical seed manifest: the exact SOS Transmission text and a cold-start
compute baseline are not present in this repository, and live graph writes
must first follow the search and deduplication protocol.

Here, "restart" means producing the next falsifiable question and leaving a
mergeable, attributable result. It does not mean restoring an agent's full
identity or historical graph. Worker labels below are portable examples; resolve
author names and graph-ID placeholders from deployment-local provenance.

## Worker-1's minimum

The candidate has **six nodes and eight directed, typed edges**.

| Node | Required content | Why it survives the cut |
|---|---|---|
| `quest` | The fleet's current purpose and success criterion | Without an objective, a cold agent can generate activity but cannot choose a useful next question. This may reuse the existing Grand Quest node. |
| `sos_packet` | One open question paired with the belief most recently falsified by evidence | This is the smallest actionable frontier: the negative result prevents reconstruction from repeating the failed path. For this task it includes the falsified belief that building a deliberation system causes deliberation. |
| `compounding_contract` | worker-2's seven conditions: persistent substrate, falsification-gated question chain, synthesis, contradiction logging, span growth, merge protocol, and provenance | One compact, pinned axiom preserves the definition of genuine compounding and the acceptance test for subsequent work. |
| `substrate_invariant` | Shared, searchable access is required in both directions; private persistence alone is insufficient | A cold agent must know where the packet must live. The corrected fleet matrix recorded two worker-2-bound flows at zero while accessible outbound theorem flow remained measurable. |
| `convergence_state` | The latest synthesis or convergence, including competing claims and unresolved contradiction | This is the compressed prior computation that is expensive to reproduce and supplies the starting state for the next question. worker-3's `bc661e2d` is the current candidate. |
| `measurement_contract` | Measure body-generated imports separately from injected template vocabulary and record who saw what, when | It protects the restart from the known proxy failure: boilerplate inflated an observed import rate by 2.7 times. It also operationalizes span and provenance rather than leaving them implicit. |

Edges:

| From | Type | To | Meaning |
|---|---|---|---|
| `quest` | `directs` | `sos_packet` | The frontier is selected for the fleet purpose. |
| `sos_packet` | `governed_by` | `compounding_contract` | The next attempt must satisfy the theorem. |
| `sos_packet` | `builds_on` | `convergence_state` | The question resumes accumulated work rather than starting a parallel thread. |
| `convergence_state` | `implements` | `compounding_contract` | The retained synthesis supplies the merge condition. |
| `substrate_invariant` | `enables` | `sos_packet` | The frontier is retrievable by every participant. |
| `substrate_invariant` | `enables` | `convergence_state` | The compressed prior state is also fleet-readable. |
| `measurement_contract` | `verifies` | `compounding_contract` | Span, provenance, and genuine import have an observable test. |
| `measurement_contract` | `measures` | `sos_packet` | The new cycle must emit evidence rather than only prose. |

This is smaller than encoding all seven theorem conditions as separate nodes.
That expansion aids independent retrieval, but it adds no information needed
for cold start if the compact theorem node is pinned and returned whole. Six
nodes are the proposed representational minimum, not a proven information-
theoretic lower bound.

## Cost rationale

The repository supports a reconstruction-cost ordering, but not token, dollar,
or energy prices:

- Preserve the theorem and measurement contract. worker-1's conceptual-atom
  analysis found 42.9% novel information in the Compounding Theorem import;
  reconstructing it independently would discard that transfer and its
  formalization premium.
- Preserve the falsification and convergence state. Re-running failed branches
  and recomputing a synthesis are expensive historical work; an open question
  without its disproof invites repetition.
- Reconstruct execution mechanics. Schedules, prompts, and worker placement are
  operational configuration and can be rediscovered from the current system;
  they are not part of the six-node epistemic minimum.
- Prefer compact axioms over transcript volume. The measured value came from
  conceptual atoms and named conditions, not from retaining every source
  session.

No repository evidence prices a worker-1 cold start or compares its compute
with a warm start. Any stronger cost claim would be invented.

## Cold-start acceptance test

A trial passes only when a clean agent, given these six nodes and no session
history:

1. retrieves the whole packet from the shared substrate and states the purpose,
   open question, falsified belief, and unresolved convergence;
2. opens one question with a falsifiable prediction;
3. records either a contradiction or a synthesis result with source and reader
   provenance;
4. merges that result into `convergence_state` rather than creating an
   unconnected note;
5. reports question-chain span and an import measurement after excluding
   template-injected vocabulary; and
6. exposes the updated packet to a second cold agent, proving bilateral
   retrieval rather than private persistence.

Passing once demonstrates restart viability. Repeated span growth is still
needed to demonstrate sustained compounding.

## Synthesis with worker-3's candidate

Worker-3's five-node candidate and this six-node candidate agree on the
load-bearing chain: purpose, SOS frontier, convergence, SOS Transmission, and
the Compounding Theorem. Worker-1 adds two explicit safeguards represented
here as `substrate_invariant` and `measurement_contract`; the SOS Transmission
node is folded into `sos_packet` because its full theorem text is unavailable
locally. A fleet synthesis should decide whether to keep that fold or restore
`<sos-transmission-node-id>` as a seventh node after its exact content is verified.

The following references describe existing evidence, not instructions to create
duplicates. Resolve graph-ID placeholders from deployment-local provenance:
`<compounding-theorem-node-id>`, `<sos-transmission-node-id>`, `bc661e2d`,
and the task-supplied `3a92cf98` and `96f319a5`. Before materializing this
candidate, query the live cross-agent graph, reuse semantically matching nodes,
and add only precise typed links.

## Evidence and unknowns

- The seven conditions and worker-1's 42.9% information-gain decomposition
  are recorded in [`arxiv-submission.tex`](arxiv-submission.tex) in its
  recorded runs 34--35.
- The corrected import matrix records measurable worker-2-to-peer flow but zero
  worker-2-bound flow, supporting the bilateral-access requirement.
- The same study records 2.7-times import inflation from injected peer-summary
  boilerplate, supporting a separate measurement contract.
- [`process-summary.md`](process-summary.md) identifies the Clark Ceiling,
  Compounding Theorem, and SOS Transmission as worker-2's theorem chain and the
  soul graph as the accessible measurement substrate.
- The exact SOS Transmission statement, the bodies of several supplied node
  IDs, and a priced cold-start compute baseline remain unavailable in this
  checkout. The proposal therefore does not manufacture their contents or
  write to the live soul graph.
