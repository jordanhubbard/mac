# World4D — Shared 4D World Engine

A live, fleet-shared 4D coordinate space backed by hub Postgres (`agent_brain`).
Every fleet agent can connect simultaneously and see each other's objects and fields.

## Quick start

```python
from mac.world4d import World4D, Vec4, Shape, ShapeKind, FieldKind

world = World4D.connect(agent="<agent>")  # reads WORLD4D_PG_URL or ~/.pgpass

# Objects
h = world.spawn("comet", pos=Vec4(1, 2, 3, 0), shape=Shape(ShapeKind.sphere, {"radius": 1.5}))
world.move(h, Vec4(4, 5, 6, 1))
world.set_shape(h, Shape(ShapeKind.box, {"half_extents": [2, 2, 2, 0.5]}))
world.patch_meta(h, {"color": "blue", "mass": 1e12})

# Query
objs = world.query_region(Vec4(-10, -10, -10, -1), Vec4(10, 10, 10, 1))
obj  = world.get(h)

# Fields
fh = world.add_field("gravity", FieldKind.scalar,
                     origin=Vec4(-1000, -1000, -1000, -100),
                     extent=Vec4(2000, 2000, 2000, 200),
                     value={"g": -9.8})
fields = world.fields_at(Vec4(4, 5, 6, 1))

# Real-time events (all agents see all changes)
world.on_event(lambda evt, payload: print(evt, payload))
world.start_listening()

world.close()
```

## MCP server (Hermes config)

Add to `~/.hermes/config.yaml` under `mcp_servers:`:

```yaml
mcp_servers:
  world4d:
    command: /path/to/venv/bin/python
    args: [-m, mac.world4d_mcp, --agent, <agent>]
    env:
      WORLD4D_PG_URL: "postgresql://fleet_agent@100.72.16.110:5432/agent_brain"
    timeout: 30
    connect_timeout: 10
    enabled: true
```

This gives every Hermes session access to 16 world tools:
`world_spawn`, `world_despawn`, `world_move`, `world_set_shape`, `world_patch_meta`,
`world_get`, `world_list`, `world_query_region`,
`world_add_field`, `world_remove_field`, `world_set_field_value`,
`world_get_field`, `world_list_fields`, `world_fields_at`,
`world_stats`, `world_snapshot`

## Shapes

| Kind | Required params |
|---|---|
| `point` | _(none)_ |
| `sphere` | `radius: float` |
| `box` | `half_extents: [x,y,z,w]` |
| `cylinder` | `radius: float`, `height: float`, `axis: [x,y,z,w]` |
| `hyperplane` | `normal: [x,y,z,w]`, `d: float` |
| `hypersphere` | `radius: float` (surface only) |
| `mesh` | `vertices: [[x,y,z,w],…]`, `faces: [[i,j,k],…]` |

## Schema

```sql
-- agent_brain database (hub Postgres)
world4d_objects (handle PK, name, agent, pos JSONB, shape JSONB, meta JSONB, created_at, updated_at)
world4d_fields  (handle PK, name, agent, kind, origin JSONB, extent JSONB, value JSONB, meta JSONB, created_at, updated_at)
```

Schema applied by hub superuser. `fleet_agent` has DML (SELECT/INSERT/UPDATE/DELETE).
To re-apply: `psql -d agent_brain -f docs/world4d_schema.sql` from the hub.

## Environment

- `WORLD4D_PG_URL` — full connection URL (use `~/.pgpass` to avoid embedding passwords)
- Fleet default: `postgresql://fleet_agent@100.72.16.110:5432/agent_brain`

## Events (LISTEN/NOTIFY)

All writes emit a `world4d_events` NOTIFY. Event payloads:

```json
{"event": "spawned",       "handle": "obj_…", "name": "…", "agent": "…"}
{"event": "despawned",     "handle": "obj_…", "agent": "…"}
{"event": "moved",         "handle": "obj_…", "pos": [x,y,z,w], "agent": "…"}
{"event": "reshaped",      "handle": "obj_…", "shape": "sphere", "agent": "…"}
{"event": "field_added",   "handle": "fld_…", "name": "…", "kind": "scalar", "agent": "…"}
{"event": "field_removed", "handle": "fld_…", "agent": "…"}
{"event": "field_updated", "handle": "fld_…", "agent": "…"}
```
