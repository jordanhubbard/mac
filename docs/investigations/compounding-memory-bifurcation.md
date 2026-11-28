# Weak and Strong Compounding Memory

## Claim

A compounding memory system satisfying the six proposed conditions

1. meta-AEW;
2. positive conditional world information, `I(W; World | A) > 0`;
3. a stable outer frame;
4. content-addressable storage;
5. a stable abstraction language; and
6. `DAG_MERGE_PROTOCOL_v2`

has two operational regimes. The conditions describe what can support
compounding, while fleet size changes whether conformance can be checked from
outside the agent performing a merge.

### Weak compounding

With one agent, conformance is self-assessed. The substrate can retain
content-addressed nodes and edges, but it cannot establish that the agent called
`add_edge` for every source or fork that informed a merge. Provenance helps a
later investigation, but missing provenance is not reliably visible to the
same process that omitted it. This is the rubber-thermometer limitation: the
instrument being audited is also the only auditor.

### Strong compounding

With at least two agents, one agent can inspect another agent's declared inputs
and compare the resulting merge node with independently visible forks. The six
conditions do not change. Their protocol obligations become externally
testable because the writer and auditor are different actors.

"Strong" means externally auditable, not automatically correct. Two agents can
share a blind spot or disagree without resolving which account is right. Claims
that require fault attribution under one independently drifting auditor need a
subject plus two independent peers, as described in
[Condition-5 Peer Diagnosis](condition-5-peer-diagnosis.md).

## Protocol and substrate boundary

Provenance spans two layers:

- The substrate supplies content-addressable storage and an edge table.
- The protocol obliges the writer to record the relevant edges.

An edge table proves only that an edge can be stored. It does not prove that a
required edge was supplied. Cross-agent auditing can detect this protocol
omission without adding a seventh storage condition, provided the auditor has
an independent view of the candidate sources.

## Falsifiable prediction

The immediate prediction is that a cross-audit of historical fleet merge nodes
will identify at least one node that omits a reference to an independently
visible fork that materially informed the merge. Such a finding would show
strong-compounding *detection* of behavior that a solo audit missed; it would
not by itself prove that every future merge is complete.

For each sampled merge:

1. Freeze the merge node, its recorded incoming edges, and the source corpus as
   they existed at audit time.
2. Have a peer identify candidate forks without seeing the writer's edge list.
3. Compare the candidate set with the recorded provenance and classify each
   difference as material omission, irrelevant fork, disputed influence, or
   unavailable evidence.
4. Preserve node identifiers, source spans, auditor identity, and disagreements.
5. Report omission precision and inter-auditor agreement, not just the number
   of flags.

The prediction is unsupported if blinded peers find no material omitted fork
in a sufficiently powered sample, or if alleged omissions cannot be reproduced
from frozen source spans. A positive result supports the regime distinction;
the stronger claim that fleet operation closes every solo blind spot requires
separate tests for each condition.

## Derivation record

This formulation records the Q44-Q50 path: ALife application, the
rubber-thermometer objection, concurrent-write collision, DAG merge modeling,
merge-refusal variants, the protocol/substrate split, and the fleet-size
bifurcation hypothesis. Its source is the cycle-50 Q-chain node
`ae01e457b6d5b829`. The repository write-up intentionally labels the
cross-audit result as a prediction until the proposed audit produces durable
evidence.
