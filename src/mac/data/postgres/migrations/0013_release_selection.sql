-- The operator-selected release (task_822aada4). One row per scope; today the
-- only scope is 'fleet'. While a release is selected, a worker whose loaded
-- code is not that commit is excluded from dispatch, and the dispatcher tick
-- reports drift against it. No row means no release gate.

CREATE TABLE IF NOT EXISTS release_selection (
    scope TEXT PRIMARY KEY,
    commit_sha TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL DEFAULT '',
    converge_by TEXT,
    selected_by TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    selected_at TEXT NOT NULL,
    CHECK (length(commit_sha) = 40)
);
