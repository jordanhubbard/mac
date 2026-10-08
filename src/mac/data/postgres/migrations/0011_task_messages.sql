-- The task board: one ordered conversation per task between the coding agent
-- running it, the people watching it, and the hub. It is how a running agent
-- receives a human's direction, a teammate's answer or a status nudge without
-- its session being stopped, and how the console shows what the agent is doing
-- while it does it.
--
-- `id` is the read cursor. A reader asks for messages after the last id it
-- saw, so nothing posted while it was busy is skipped.
--
-- author_kind says who wrote a row, author says which one: an agent id, a
-- human's name, or "hub". kind is what the row is for (see mac.task_board).
-- metadata is a JSON object as TEXT, like every other JSON column here.

CREATE TABLE IF NOT EXISTS task_messages (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    author_kind TEXT NOT NULL CHECK (author_kind IN ('agent', 'human', 'hub')),
    author TEXT NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    reply_to BIGINT REFERENCES task_messages(id) ON DELETE SET NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_messages_task
    ON task_messages (task_id, id);
