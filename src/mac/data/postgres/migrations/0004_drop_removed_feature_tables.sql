-- Drop the tables of the autonomy features removed on 2026-09-30: the
-- scientific optimizer, dreaming, and nap consolidation. No code reads or
-- writes them any longer.
--
-- Children are dropped before the tables they reference, so no CASCADE is
-- needed. Without CASCADE an object nobody declared here (an operator's view,
-- say) makes the migration fail and roll back instead of being dropped
-- silently along with the table. Their indexes go with them. IF EXISTS keeps
-- this a no-op for a table that was never created.

DROP TABLE IF EXISTS scientific_decisions;
DROP TABLE IF EXISTS scientific_observations;
DROP TABLE IF EXISTS scientific_assignments;
DROP TABLE IF EXISTS scientific_experiments;
DROP TABLE IF EXISTS scientific_policies;
DROP TABLE IF EXISTS scientific_optimizer_events;
DROP TABLE IF EXISTS scientific_optimizer_locks;

DROP TABLE IF EXISTS dream_candidate_entries;
DROP TABLE IF EXISTS dream_runs;

DROP TABLE IF EXISTS nap_runs;
DROP TABLE IF EXISTS nap_schedules;
