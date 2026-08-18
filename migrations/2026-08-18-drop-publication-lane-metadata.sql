-- Strip the dead publication lane/route keys from task metadata.
--
-- WHY. `publication_lane` was a two-valued field with one reachable value:
-- `managed` required a work-package link, every work_package_* table on the
-- live hub was empty, and the single-task call site passed
-- `package_linked=False` unconditionally. The work-package pipeline was
-- removed in #413; the lane went with the CLI column that printed it on every
-- task. `publication_route` went too -- with the lane gone it was a constant
-- husk whose remaining fields (package_id, plan_version, epoch,
-- landing_receipt_id, finalization_id) were always NULL work-package
-- leftovers.
--
-- NOT AUTOMATIC. Nothing in mac reads a migrations/ directory: schema.sql is
-- applied at hub start and is CREATE TABLE IF NOT EXISTS only, and
-- pyproject.toml packages src/mac, so this file is not even shipped in the
-- wheel. Run it by hand:
--
--     psql "$MAC_DB" -f migrations/2026-08-18-drop-publication-lane-metadata.sql
--
-- SAFE TO SKIP AND SAFE TO REPEAT. Nothing reads these keys any more, so
-- leaving them costs only bytes; the operator/read `- ?` guard makes a second
-- run a no-op. Measured on the live hub 2026-08-18: 129 of 8,187 task rows
-- carry them.
--
-- There is no COLUMN to drop. These were always JSON keys inside
-- tasks.metadata, never a schema column -- the CLI's "LANE" column was
-- rendered from the metadata key.

BEGIN;

UPDATE tasks
   SET metadata = (metadata::jsonb - 'publication_lane' - 'publication_route')::text
 WHERE metadata::jsonb ?| array['publication_lane', 'publication_route'];

COMMIT;

-- Verify (expect 0):
--   SELECT count(*) FROM tasks
--    WHERE metadata::jsonb ?| array['publication_lane','publication_route'];
