-- Move the lazily-created dream candidate store under versioned schema
-- authority. The IF NOT EXISTS clauses also adopt installations where a dream
-- command created these tables before Stage 1A was deployed.
CREATE TABLE IF NOT EXISTS dream_runs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    state TEXT NOT NULL,
    agent_id TEXT,
    project TEXT,
    extractor TEXT,
    policy TEXT NOT NULL,
    gates TEXT NOT NULL,
    stats TEXT NOT NULL,
    reflections TEXT NOT NULL,
    errors TEXT NOT NULL,
    input_record_count INTEGER NOT NULL DEFAULT 0,
    input_session_count INTEGER NOT NULL DEFAULT 0,
    created_by TEXT,
    created_at TEXT NOT NULL,
    promoted_at TEXT
);

CREATE TABLE IF NOT EXISTS dream_candidate_entries (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    statement TEXT NOT NULL,
    scope TEXT,
    project TEXT,
    agent_id TEXT,
    applies_when TEXT,
    confidence TEXT,
    confidence_score DOUBLE PRECISION,
    source_count INTEGER,
    sources TEXT,
    supersedes TEXT,
    contradicts TEXT,
    promoted_memory_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dream_entries_run
    ON dream_candidate_entries (run_id);
CREATE INDEX IF NOT EXISTS idx_dream_runs_state
    ON dream_runs (state, created_at);
