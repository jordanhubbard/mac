-- Migration 0014: add deliberations table
-- Schema v3 (exploration run 20): authorized_resolvers derived from domain ownership
-- arguments stored as JSONB within the deliberation record (not a separate table)

CREATE TABLE IF NOT EXISTS deliberations (
    id TEXT PRIMARY KEY,
    task_id TEXT,                           -- the task that caused this deliberation
    topic TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'design',   -- design | operations | policy
    body TEXT NOT NULL DEFAULT '',          -- working reasoning, free-form
    conclusion TEXT,                        -- the actionable output agents act on
    status TEXT NOT NULL DEFAULT 'open',    -- open | resolved | withdrawn
    created_by TEXT NOT NULL,              -- agent_id of opener
    authorized_resolvers TEXT NOT NULL DEFAULT '[]',  -- JSON list of agent_ids
    participants TEXT NOT NULL DEFAULT '[]',  -- JSON: [{agent_id, initial_position, final_position, vote, moved_by, joined_at}]
    arguments TEXT NOT NULL DEFAULT '[]',   -- JSON: [{id, agent_id, claim, argument_type, created_at}]
    resolved_by TEXT,                       -- agent_id who resolved
    resolution_path TEXT,                   -- "authority" | "unanimity" | "expiry"
    expires_at TEXT,                        -- ISO-8601 optional TTL
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS deliberations_task_id ON deliberations (task_id);
CREATE INDEX IF NOT EXISTS deliberations_status ON deliberations (status);
CREATE INDEX IF NOT EXISTS deliberations_created_by ON deliberations (created_by);
