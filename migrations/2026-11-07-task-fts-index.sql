-- Add GIN index on tsvector over task title+description for fast full-text search.
-- The services.py PG fast-path (websearch_to_tsquery) already exists but runs a
-- sequential scan without this index. This makes it O(log N) instead of O(N).

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tasks_fts
    ON tasks
    USING GIN (
        to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, ''))
    );
