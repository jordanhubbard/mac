-- Authorized legacy prune drops work_package_* TABLES CASCADE. That does not
-- drop trigger functions that live on `tasks` and only *query* those tables.
-- Observed 2026-08-27 on the live hub after
-- MAC_DEPLOY_AUTHORIZE_LEGACY_SCHEMA_PRUNE=1: every claim_task_v2 failed with
-- `relation "work_package_assignment_audit" does not exist` because
-- trg_work_package_task_claim_authority was still attached to tasks.
--
-- IF EXISTS / CASCADE so this is a no-op on a fresh database and on a hub
-- whose operator already ran migrations/2026-08-17-drop-work-package-tables.sql.

DROP TRIGGER IF EXISTS trg_work_package_expiry_task_detach_guard ON tasks;
DROP TRIGGER IF EXISTS trg_work_package_task_claim_authority ON tasks;

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
