# Soul Graph → Postgres Migration

Each agent maintains a local soul graph in `soul.json` (served by `mac.soul_mcp`).
The shared Postgres database on each agent's host provides persistence, full-text
search, pgvector embeddings, and an `agent` column so all three graphs coexist in
one schema.

## Schema

`soul_nodes` and `soul_edges` tables in the agent's local Postgres container.
An `agent` column (added by the migration script, idempotent) tags every node
with the owning agent name.

## Running the migration

### Natasha (sparky — this machine)

```bash
~/.mac/venv/bin/python scripts/soul_pg_migrate.py \
  --agent natasha \
  --soul-file ~/.hermes/soul.json \
  --sqlite ~/AgentFS/natasha-memory-graph/memory_graph.db \
  --pg-url "postgresql://natasha@127.0.0.1:5433/natasha"
```

Postgres container: `docker ps | grep natasha-pg` (127.0.0.1:5433 → natasha/natasha_local)

### Rocky (puck.local — hub)

Rocky needs to run this from their own host against their own Postgres.
If Rocky's Postgres is the hub instance (127.0.0.1:5432, db=mac), use:

```bash
~/.mac/venv/bin/python scripts/soul_pg_migrate.py \
  --agent rocky \
  --soul-file ~/.hermes/soul.json \
  --pg-url "postgresql://jkh@127.0.0.1:5432/soul_graph"
```

No `--sqlite` needed unless a legacy `memory_graph.db` exists on rocky.

### Bullwinkle (madmax — 100.121.27.109)

```bash
~/.mac/venv/bin/python scripts/soul_pg_migrate.py \
  --agent bullwinkle \
  --soul-file ~/.hermes/soul.json \
  --pg-url "postgresql://<user>@127.0.0.1:<port>/<db>"
```

Confirm Bullwinkle's Postgres details with `docker ps` on madmax first.

## Idempotency

Re-running the script is safe:
- Nodes with the same ID are merged (tags union, pinned OR'd), content preserved
- Nodes with duplicate content under a different ID are merged to the existing ID
- Edges are upserted with `ON CONFLICT DO NOTHING`
- The `agent` column is added with `IF NOT EXISTS`

## pg_hba.conf note

The natasha-pg container's `pg_hba.conf` has been updated to trust connections
from the Docker bridge subnet (`172.17.0.0/16`). This is required for host-side
psycopg connections routed through the Docker bridge gateway. The rule sits
above the `scram-sha-256` catch-all.
