-- Drop mac's native speculative merge queue. Approved work now lands through a
-- serial land loop per repository (ControlPlane._publish_git_target_if_needed),
-- which keeps no queue state of its own: the canonical tip and the task's
-- metadata.landing budget are the whole record. No code reads or writes these
-- tables any longer.
--
-- No CASCADE, as in 0004: an object nobody declared here makes the migration
-- fail and roll back instead of being dropped silently with the table. The
-- indexes go with their table. IF EXISTS keeps this a no-op for a table that
-- was never created.

DROP TABLE IF EXISTS merge_queue_entries;
DROP TABLE IF EXISTS merge_queue_windows;
