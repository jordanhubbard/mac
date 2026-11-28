# Condition-6 Convergence Protocol

## Purpose

Condition 6 of the Compounding Theorem requires an explicit merge when two
independent fleet exploration threads reach the same finding. Similar search
results are not a merge: a durable convergence node must preserve both inputs,
the shared claim, any residual disagreement, and the next question.

## Detection Pattern

Treat two findings as a convergence candidate only when all of these checks
pass:

1. **Distinct authors:** the findings were produced by different agent roles.
2. **Independent paths:** neither finding cites the other, and their provenance
   chains have no shared finding after the declared independence cutoff.
3. **Same testable claim:** a reviewer can state one normalized claim entailed
   by both findings. Keyword or embedding similarity alone is insufficient.
4. **Compatible evidence:** the findings do not depend on contradictory
   measurements or incompatible scopes. Qualify the shared claim when scopes
   differ rather than hiding the difference.
5. **No prior merge:** search for a convergence node whose `input_ids` already
   contain the pair. Extend that node if the synthesis changed; do not create a
   duplicate.

Detection can use same-thread retrieval, reciprocal nearest-neighbour search,
or matching normalized claims to propose pairs. A proposal becomes a merge
only after checking source bodies and provenance. If the common claim is not
substantive, retain the findings separately.

## Convergence Node Schema

Every convergence node has these required fields:

| Field | Contract |
|---|---|
| `id` | Stable identifier for this merge |
| `type` | Literal `convergence` |
| `claim` | The normalized, testable claim shared by both inputs |
| `input_ids` | At least two finding IDs |
| `input_authors` | Distinct fleet-generic author roles corresponding to the inputs |
| `independence` | Cutoff plus evidence that neither path derived from the other |
| `provenance` | Retrievable source locations for every input |
| `agreement` | What the inputs establish jointly |
| `residuals` | Differences, contradictions, or unavailable source detail |
| `next_question` | One question made possible by the merge |
| `created_at` | ISO-8601 UTC creation time |

The node links to each input with `converges_with`. Later findings use
`builds_on`; evidence that invalidates the synthesis uses `contradicts`. Input
nodes remain intact so the merge is auditable and reversible.

## Cross-Agent Instance

The repository contains a real convergence between two independently authored
SOS-bootstrap proposals. One exploration path produced soul node `bc661e2d`,
described as a five-node candidate. A separate path produced the six-node
candidate in [`fleet-sos-bootstrap.md`](fleet-sos-bootstrap.md). That document
records their agreement on the load-bearing chain and also records the second
path's two additional safeguards. The merge below makes the previously prose-
only cross-agent convergence explicit without claiming access to unavailable
node bodies.

```yaml
id: convergence:sos-bootstrap:five-six-node
type: convergence
claim: >-
  A minimum SOS bootstrap must preserve purpose, the current frontier, a
  convergence state, transmission context, and the Compounding Theorem.
input_ids:
  - bc661e2d
  - repo:docs/investigations/fleet-sos-bootstrap.md#bullwinkles-minimum
input_authors:
  - agent-research-a
  - agent-research-b
independence:
  cutoff: 2026-11-28T00:00:00Z
  evidence: >-
    The repository proposal identifies the five-node candidate as a separate
    proposal and compares it with its own six-node derivation.
provenance:
  - docs/investigations/arxiv-paper-v0.md#appendix-b-key-soul-node-ids
  - docs/investigations/fleet-sos-bootstrap.md#synthesis-with-natashas-candidate
agreement: >-
  Both paths retain the same load-bearing chain: purpose, frontier,
  convergence, transmission, and the Compounding Theorem.
residuals:
  - The six-node path makes substrate and measurement safeguards explicit.
  - The exact body of bc661e2d is unavailable in this checkout.
  - The transmission theorem may be folded into the packet or restored after verification.
next_question: >-
  Do the two extra safeguards belong in the minimum graph or in its acceptance test?
created_at: 2026-11-28T00:00:00Z
```

This instance passes the distinct-author and normalized-claim checks based on
the cited repository evidence. Its independence evidence is documentary, not
a cryptographic proof; discovery of a pre-cutoff citation between the paths
would invalidate the independence classification and require relabelling the
node as a synthesis rather than convergence.

## Acceptance And Falsification

A condition-6 merge is complete only when the node is durable and searchable,
both inputs resolve, the authors are distinct, and a new exploration can cite
the convergence node instead of choosing one source silently. The merge fails
if either input is missing, the common claim cannot be derived from both source
bodies, independence is false, or residual contradictions were omitted.
