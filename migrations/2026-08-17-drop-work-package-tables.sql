-- Drop the work-package pipeline's tables from a live hub.
--
-- WHY THIS FILE EXISTS SEPARATELY: `src/mac/data/postgres/schema.sql` is
-- CREATE TABLE IF NOT EXISTS, applied on hub start (open_postgres_store
-- defaults to initialize_schema=True). That is enough to ADD a new table on
-- restart, but it is purely additive: it has no DROP and no ALTER, so
-- REMOVING a table from schema.sql only stops it being created on a fresh
-- database. Every table below therefore still exists on the live hub after
-- the code change lands, holding whatever it held before.
--
-- SAFETY -- what was actually checked, and what was not:
--
--   * The `work_package_*` tables (20+ of them) were verified empty on the
--     live hub via pg_stat_user_tables. That is the observation this whole
--     removal rests on.
--   * `evidence_attempt_links` / `evidence_attempt_verifications` were NOT
--     independently counted. They are included because their only writer was
--     the package branch of `ControlPlane.add_evidence`, which required a
--     `work_package_assignment_audit` row -- so they cannot hold more than
--     those empty tables did. CONFIRM WITH A COUNT BEFORE RUNNING THIS.
--   * `execution_cohort_assignments` and `execution_cohort_configurations`
--     DO hold live rows (roughly one per created task, ~8k). They recorded a
--     managed-vs-legacy publication A/B experiment. Every row reads
--     `treatment_route = 'legacy_async'` because the managed arm was
--     unreachable, so there is no comparison in the data -- but it is real
--     data and dropping it is a real deletion.
--
-- Count first:
--
--   SELECT relname, n_live_tup FROM pg_stat_user_tables
--    WHERE relname LIKE 'work\_package%' OR relname LIKE 'evidence\_attempt%'
--       OR relname LIKE 'execution\_cohort%' ORDER BY relname;
--
-- Then dump the cohort tables if you want the record:
--
--   pg_dump -t execution_cohort_assignments -t execution_cohort_configurations \
--     "$MAC_DB" > execution-cohort-archive.sql
--
-- Run this only AFTER the hub is running the code that no longer references
-- these tables. CASCADE removes the dependent triggers, indexes, and foreign
-- keys; no surviving table references any of them.

BEGIN;

DROP TABLE IF EXISTS evidence_attempt_verifications CASCADE;
DROP TABLE IF EXISTS evidence_attempt_links CASCADE;
DROP TABLE IF EXISTS execution_cohort_assignments CASCADE;
DROP TABLE IF EXISTS execution_cohort_configurations CASCADE;
DROP TABLE IF EXISTS work_package_assignment_audit CASCADE;
DROP TABLE IF EXISTS work_package_batch_inputs CASCADE;
DROP TABLE IF EXISTS work_package_certification_jobs CASCADE;
DROP TABLE IF EXISTS work_package_certifications CASCADE;
DROP TABLE IF EXISTS work_package_controller_outcomes CASCADE;
DROP TABLE IF EXISTS work_package_controller_station_receipts CASCADE;
DROP TABLE IF EXISTS work_package_epochs CASCADE;
DROP TABLE IF EXISTS work_package_finalization_outcomes CASCADE;
DROP TABLE IF EXISTS work_package_history CASCADE;
DROP TABLE IF EXISTS work_package_integration_batches CASCADE;
DROP TABLE IF EXISTS work_package_landing_attempts CASCADE;
DROP TABLE IF EXISTS work_package_landing_intents CASCADE;
DROP TABLE IF EXISTS work_package_landing_receipts CASCADE;
DROP TABLE IF EXISTS work_package_landing_streams CASCADE;
DROP TABLE IF EXISTS work_package_lease_expiry_repairs CASCADE;
DROP TABLE IF EXISTS work_package_node_candidates CASCADE;
DROP TABLE IF EXISTS work_package_node_lineage CASCADE;
DROP TABLE IF EXISTS work_package_plan_versions CASCADE;
DROP TABLE IF EXISTS work_package_publication_finalizations CASCADE;
DROP TABLE IF EXISTS work_package_ref_retirement_attempts CASCADE;
DROP TABLE IF EXISTS work_package_ref_retirement_intents CASCADE;
DROP TABLE IF EXISTS work_package_ref_retirement_receipts CASCADE;
DROP TABLE IF EXISTS work_package_station_attempts CASCADE;
DROP TABLE IF EXISTS work_package_task_links CASCADE;
DROP TABLE IF EXISTS work_package_telemetry_health CASCADE;
DROP TABLE IF EXISTS work_package_wip_tokens CASCADE;
DROP TABLE IF EXISTS work_packages CASCADE;

-- Trigger functions the dropped triggers used. CASCADE on the tables removes
-- the triggers but leaves these function definitions behind.
DROP FUNCTION IF EXISTS trg_evidence_attempt_links_immutable() CASCADE;
DROP FUNCTION IF EXISTS trg_evidence_attempt_package_identity() CASCADE;
DROP FUNCTION IF EXISTS trg_evidence_attempt_verification_identity() CASCADE;
DROP FUNCTION IF EXISTS trg_evidence_attempt_verifications_immutable() CASCADE;
DROP FUNCTION IF EXISTS trg_execution_cohort_append_only() CASCADE;
DROP FUNCTION IF EXISTS trg_execution_cohort_configuration_append_only() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_assignment_immutable() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_batch_fence_monotonic() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_batch_initial_state() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_batch_inputs_open() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_batch_invariants() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_batch_repository_matches() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_certification_job_lifecycle() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_certification_lifecycle() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_controller_outcome_append_only() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_controller_station_lifecycle() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_current_epoch_status() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_epochs_lifecycle() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_expiry_node_guard() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_expiry_repair_authority() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_expiry_repairs_immutable() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_expiry_task_detach_guard() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_finalization_outcome_append_only() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_history_immutable() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_landing_attempt_lifecycle() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_landing_intent_lifecycle() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_landing_receipt_lifecycle() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_landing_stream_invariants() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_lineage_carry_forward_evidence() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_node_candidate_lifecycle() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_node_lineage_immutable() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_plan_versions_immutable() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_publication_finalization_lifecycle() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_ref_retirement_append_only() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_station_attempt_append_only() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_task_claim_authority() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_task_link_candidate_state() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_task_link_executable_insert() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_task_links_identity_immutable() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_task_links_lifecycle() CASCADE;
DROP FUNCTION IF EXISTS trg_work_package_wip_lifecycle() CASCADE;
DROP FUNCTION IF EXISTS trg_work_packages_current_epoch_coherent() CASCADE;
DROP FUNCTION IF EXISTS trg_work_packages_initial_state() CASCADE;
DROP FUNCTION IF EXISTS trg_work_packages_state_transition() CASCADE;

COMMIT;

-- Reclaim the space the dropped relations held (outside the transaction).
-- VACUUM (VERBOSE, ANALYZE);
