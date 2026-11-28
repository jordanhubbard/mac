# Soul graph sync to fleet_brain

`mac.soul_pg_sync` is the one-shot bridge between a local Hermes soul graph and
the `fleet_brain.soul_nodes` and `fleet_brain.soul_edges` tables. It is distinct
from the live `SoulGraphPG` backend documented in
`docs/fleet-soul-graph-pg-setup.md`; that backend targets the newer
`agent_brain` schema, whose ownership column is named `agent` rather than
`agent_id`.

## Worker-2 import path

The first worker agent's original nodes and parent edges followed this data path:

1. Read the local JSON graph, historically
   `~/.hermes/memories/<agent>_soul.json`.
2. Convert each key in the JSON `nodes` object into a `soul_nodes` row.
3. Convert every node's `parents` entries into `soul_edges` rows directed from
   parent to child.
4. Upsert the rows with `agent_id='<agent>'` into `fleet_brain` in one
   transaction.

That path is implemented by `mac.soul_pg_sync.json_to_pg`. The import is safe
to repeat: nodes use `ON CONFLICT (node_id, agent_id) DO UPDATE`, and duplicate
edges use `ON CONFLICT ... DO NOTHING`.

## Hub and GPU-worker agents

Run the same importer on each agent's own host so that the local soul path and
database credentials never need to be copied between hosts. Supply the short
fleet identity explicitly; this value is written unchanged to every imported
node and edge.

```console
python -m mac.soul_pg_sync import --agent <hub-agent> \
  --soul /path/to/<hub-agent>/soul.json \
  --db-url postgresql:///fleet_brain

python -m mac.soul_pg_sync import --agent <gpu-worker> \
  --soul /path/to/<gpu-worker>/soul.json \
  --db-url postgresql:///fleet_brain
```

Use `DATABASE_URL` instead of `--db-url` when the credential is supplied by the
runtime environment. Do not put a password-bearing URL in shell history or a
checked-in file.

Verify each import independently:

```console
python -m mac.soul_pg_sync status --agent <hub-agent> --db-url postgresql:///fleet_brain
python -m mac.soul_pg_sync status --agent <gpu-worker> --db-url postgresql:///fleet_brain
```

The status output reports database node and edge counts and, when `--soul` is
provided, the local node count. A successful deployment has non-zero database
counts for the requested identity and an expected local/database node count.
