-- Per-task inference tokens. A worker mints one for each task sandbox so the
-- coding CLI inside it can call the hub's model router (POST
-- /v1/chat/completions and /v1/embeddings) and nothing else. The worker's own
-- token never enters the sandbox: it can claim tasks and write the ledger.
--
-- A separate table rather than rows in worker_credentials: activating a worker
-- token supersedes every other live row for the agent, which would cut off a
-- running task's inference, and one row per task would bloat the worker's
-- credential versions and `mac admin worker-token list`.
--
-- Only the sha256 hash of a token is stored. Timestamps are fixed-width UTC
-- text (YYYY-MM-DDTHH:MM:SS.ffffffZ), so `expires_at > ?` compares correctly.
-- Rows past expiry no longer authenticate and are pruned when the next token
-- is minted.

CREATE TABLE IF NOT EXISTS inference_tokens (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE RESTRICT,
    token_hash TEXT NOT NULL UNIQUE,
    token_fingerprint TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inference_tokens_expiry
    ON inference_tokens (expires_at);
CREATE INDEX IF NOT EXISTS idx_inference_tokens_agent
    ON inference_tokens (agent_id, expires_at);
