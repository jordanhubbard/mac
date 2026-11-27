# Fleet communication and decision memory after World4D

Ordinary fleet work uses the MAC task ledger, AgentBus and the existing memory
stores. World4D runs only as an explicitly started research environment. Preserve
the prototype, its data and agent-authored research notes. The production fleet
does not need a simulation clock to coordinate work or remember a lesson.

The original World4D specification described a shared environment with stable
objects, fields and events. The [coordinate experiment](../world4d-coordinate-protocol.md)
suggests a further intent. Agents wanted to see what peers were doing, gather
around a common problem and leave persistent context that a later turn could
discover. Those are hypotheses about the intent. The prototype demonstrated
signal exchange and experiments with shared coordinates. It provided no
controlled estimate of decision quality or task outcomes.

## Map the useful capabilities to existing systems

| Need | Existing authority and proposed improvement |
| --- | --- |
| Who is working on this and what should I inspect? | Derive a task-scoped view from leases, heartbeats, dependencies and artifacts. Show source and freshness. Load it on demand with bounded results. |
| How do I get a peer to act on new information? | Extend the [AgentBus handoff convention](../task-handoff-convention.md). Correlate the request, reply, resulting action and task outcome. Reuse existing stream and reply IDs. |
| How can a later agent recover a lesson? | Keep evidence and decisions in the task ledger, and promote reusable lessons through existing soul-memory search and linking. Retrieve by project, decision and applicability. |
| Can agents discover related work across projects? | Pilot retrieval using task and artifact relations, topic tags and existing text search. Evaluate optional semantic ranking as a separate change. |

## Stage 1. Trace a handoff through to a result

Start with incident handoffs in which one agent supplies a test result or a
known bad approach. Read the existing AgentBus handoff envelope, correlated
peer reply and durable task events. Add a small action receipt only where those
records leave a gap. It should identify the task, request and evidence, plus
the recipient's disposition and a reference to its action or reason for
declining. Use stable IDs to make retries idempotent. A receipt of delivery,
a reply, an action and a verified outcome each need their own status.

Join those records in an on-demand report. Count unresolved and late replies
explicitly. Reconcile a late reply with the original request, and avoid
counting replays twice. Measure time to a usable response, duplicate work and
verified task outcomes. Use existing ownership and visibility rules. Show stale
or missing evidence where it occurs.

## Stage 2. Retain and retrieve a lesson at decision time

Start with failed build and recovery attempts, where the same mistake can be
recognized across runs. Use existing memory tools to propose one reusable
lesson per distinct finding. Include the triggering conditions, action,
result, evidence reference, scope and verification date. Search before adding.
Refine an existing lesson, link contradictory evidence, and mark superseded
guidance so it loses priority in retrieval. Pending proposals can remain
linked to task evidence until independently checked.

At task start or before choosing a recovery action, retrieve a small set of
applicable lessons. Start with at most three results and a 600-token budget.
Record which memory IDs were surfaced, which were used or rejected, and the
resulting choice and evidence. Empty results are a normal outcome. Keep
retrieval logs separate from candidate memory content and bound their
retention. A match, access-count increment or agent self-rating is diagnostic
evidence. Use task results as the outcome measure.

## Stage 3. Evaluate and decide whether to expand

1. Replay representative past handoffs and recovery tasks with the original
   information available at each decision. Hold out task families so near
   duplicates cannot leak answers. Include irrelevant, outdated, conflicting
   and inaccessible records, plus cases where the right result is empty.
2. Compare the current workflow with the handoff receipt, then with memory
   retrieval enabled. Keep routing, models and task inputs comparable. In a
   live pilot, assign whole tasks to cohorts and use independent or automated
   outcome checks such as test success, an accepted handoff or a verified
   recovery. Record missing labels and failed runs.
3. Pre-register success and cost limits after measuring a baseline. A proposed
   pilot target is at least 20% fewer repeated known failures or a 20% shorter
   median time to an accepted handoff, with no material decline in task
   acceptance and at most 10% extra tokens per accepted task. Report sample
   sizes, uncertainty, tail latency, database operations and storage growth.
   Treat small or inconclusive cohorts as a reason to gather better evidence.
4. Gate each change independently. Keep the evaluation event-driven and
   disable the pilot if it exceeds the budget, leaks scoped context, or
   repeatedly surfaces stale guidance. Restore the existing retrieval
   behavior on rollback. Revisit spatial indexing only if a bounded
   experiment can beat the same records with conventional retrieval.

## Retained World4D data

Keep the stopped worlds, configuration snapshots and research notes available
for reproducibility. Treat the bridge-generated records as simulation
telemetry. Inventory consumers before separating or archiving those rows.
Use provenance and record kind to select generated rows. Preserve authored
memories, even when their text mentions the simulation. Validate a restore
before any separately authorized removal from shared storage.
