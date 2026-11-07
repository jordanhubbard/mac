-- Migration 0030: Shared 4D World Environment
-- agent_brain database
-- All agents read/write the same world state.
-- Objects have: handle (UUID), agent that spawned it, position (x,y,z,t),
--   shape descriptor (jsonb), and named scalar/vector fields (jsonb).

-- ───────────────────────────────────────────────
-- world_objects: one row per object in the world
-- ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS world_objects (
    id          TEXT PRIMARY KEY,          -- handle: 'wobj_<uuid4_hex>'
    name        TEXT NOT NULL DEFAULT '',  -- human label
    agent       TEXT NOT NULL,             -- spawning agent
    -- 4D position
    x           DOUBLE PRECISION NOT NULL DEFAULT 0,
    y           DOUBLE PRECISION NOT NULL DEFAULT 0,
    z           DOUBLE PRECISION NOT NULL DEFAULT 0,
    t           DOUBLE PRECISION NOT NULL DEFAULT 0,  -- world-time (epoch seconds or logical tick)
    -- shape: {type: "box"|"sphere"|"mesh"|"point", ...geometry params}
    shape       JSONB NOT NULL DEFAULT '{"type":"point"}',
    -- arbitrary named fields: {temperature: 42.0, pressure: [1,0,0], color: "red", ...}
    fields      JSONB NOT NULL DEFAULT '{}',
    -- metadata
    tags        TEXT[] NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS world_objects_agent_idx ON world_objects(agent);
CREATE INDEX IF NOT EXISTS world_objects_tags_idx  ON world_objects USING GIN(tags);
CREATE INDEX IF NOT EXISTS world_objects_fields_idx ON world_objects USING GIN(fields);
CREATE INDEX IF NOT EXISTS world_objects_shape_idx  ON world_objects USING GIN(shape);

-- spatial index on (x,y,z) for region queries
CREATE INDEX IF NOT EXISTS world_objects_xyz_idx ON world_objects(x, y, z);

-- ───────────────────────────────────────────────
-- world_events: append-only event log (positions, field changes, etc.)
-- ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS world_events (
    id          BIGSERIAL PRIMARY KEY,
    object_id   TEXT NOT NULL REFERENCES world_objects(id) ON DELETE CASCADE,
    agent       TEXT NOT NULL,
    event_type  TEXT NOT NULL,  -- 'spawn'|'move'|'shape'|'field_set'|'despawn'|'tick'
    payload     JSONB NOT NULL DEFAULT '{}',
    world_t     DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS world_events_object_idx ON world_events(object_id);
CREATE INDEX IF NOT EXISTS world_events_type_idx   ON world_events(event_type);
CREATE INDEX IF NOT EXISTS world_events_t_idx      ON world_events(world_t);

-- ───────────────────────────────────────────────
-- world_state: singleton row holding global world clock
-- ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS world_state (
    id          TEXT PRIMARY KEY DEFAULT 'world',
    tick        BIGINT NOT NULL DEFAULT 0,
    world_t     DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO world_state (id, tick, world_t) VALUES ('world', 0, 0)
    ON CONFLICT (id) DO NOTHING;

GRANT SELECT, INSERT, UPDATE, DELETE ON world_objects TO fleet_agent;
GRANT SELECT, INSERT, UPDATE, DELETE ON world_events  TO fleet_agent;
GRANT SELECT, INSERT, UPDATE, DELETE ON world_state   TO fleet_agent;
GRANT USAGE, SELECT ON SEQUENCE world_events_id_seq  TO fleet_agent;
