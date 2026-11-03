# Task handoffs by reference

A fleet task handoff is a reference plus retrieval parameters, not a prose
summary. The sender identifies the durable task record and tells the receiver
how to retrieve its context. The receiver reads the source records directly,
so the handoff is not limited to what the sender happened to notice.

## Current MAC convention

Send a JSON AgentBus payload with this shape:

```json
{
  "schema": "mac.task_handoff.v1",
  "task_ref": "task_0123456789abcdef",
  "retrieval": {
    "substrate": "mac.memory_records",
    "task_id": "task_0123456789abcdef",
    "order": "desc",
    "limit": 20,
    "record_type_prefix": null,
    "expansion_hops": 0
  }
}
```

The fields mean:

- `task_ref` is the source task whose ledger, evidence, and memory records
  contain the handoff context.
- `substrate` names the retrieval implementation. Today the portable MAC
  substrate is `mac.memory_records`, exposed by `mac admin memory search`.
- `task_id`, `order`, `limit`, and optional `record_type_prefix` map directly
  to that command's deterministic filters.
- `expansion_hops` is `0` today. It reserves the intended fisheye-style graph
  expansion without pretending MAC's current memory query can traverse links.

Fields may be added under `retrieval`, but receivers must reject an unknown
`schema` or `substrate` rather than silently applying different semantics.
Keep credentials, private conversation memory, and raw secret-bearing output
out of both the task records and the envelope.

## Sending and receiving

Send the envelope on a direct AgentBus stream (line breaks added here only for
readability):

```console
mac admin agentbus open agent_sender \
  --recipient-agent-id agent_receiver \
  --task-id task_0123456789abcdef \
  --topic task.handoff \
  --content-type application/json \
  --stream-id handoff-task_0123456789abcdef
mac admin agentbus append handoff-task_0123456789abcdef agent_sender \
  --content-type application/json \
  --payload '{"schema":"mac.task_handoff.v1","task_ref":"task_0123456789abcdef","retrieval":{"substrate":"mac.memory_records","task_id":"task_0123456789abcdef","order":"desc","limit":20,"record_type_prefix":null,"expansion_hops":0}}' \
  --final
```

The receiver validates that `task_ref` equals `retrieval.task_id`, then runs:

```console
mac task show task_0123456789abcdef
mac --json admin memory search \
  --task-id task_0123456789abcdef \
  --order desc \
  --limit 20
```

If `record_type_prefix` is non-null, add
`--record-type-prefix <value>`. Inspect task evidence and history when the
handoff requires repository or verification detail; the task reference is the
join key for those records too.

Do not add a narrative `summary` field. A short `intent` may say what action is
requested, but it is not context and must not override the referenced records.
If retrieval fails, ask for a corrected reference or access; do not infer a
summary from the envelope.

## Relation to Qdrant and fisheye retrieval

Qdrant is already the fleet's shared semantic-memory infrastructure, but the
MAC CLI does not currently expose a stable task-ref semantic-neighborhood
query. Therefore this version uses exact task-scoped MAC records and explicitly
sets zero expansion hops. A future substrate may implement the intended
fisheye retrieval as the task itself, its linked records, one graph hop, and a
bounded semantic recall tail. That change requires a new documented substrate
name and retrieval parameters; the `mac.task_handoff.v1` envelope does not
claim those semantics today.

## Verification example

`tests/test_task_handoff_convention.py` performs a real in-memory MAC handoff:
one agent sends this envelope over AgentBus, the recipient reads it, applies
the declared task-scoped retrieval parameters, and recovers both source memory
records without receiving a prose summary.
