-- Drop the hub self-upgrade, release epoch and source convergence tables.
-- scripts/fleet-update, run by a human, replaced all three: in the 90 days
-- before it, release epochs aborted 62% of the time and hub self-upgrade never
-- succeeded. The services that owned these tables (fleet_upgrade_service,
-- hub_upgrade_supervisor, fleet_release_epoch_service, source_release_service,
-- source_convergence_service) are deleted, and no code reads or writes the
-- tables any longer.
--
-- environments goes too. 0006 kept it only because fleet_desired_source_states
-- referenced it and the source-release service read it; both are gone here.
--
-- No CASCADE, as in 0004 to 0007: an object nobody declared here makes the
-- migration fail and roll back instead of being dropped silently with the
-- table. Children are dropped before the tables they reference. Indexes and
-- triggers go with their table; the two trigger functions do not, so they are
-- dropped explicitly afterwards. IF EXISTS keeps this a no-op for an object
-- that was never created.

DROP TABLE IF EXISTS fleet_upgrade_events;
DROP TABLE IF EXISTS fleet_upgrades;
DROP TABLE IF EXISTS source_convergence_nodes;
DROP TABLE IF EXISTS source_convergence_controller_leases;
DROP TABLE IF EXISTS fleet_release_attestation_candidates;
DROP TABLE IF EXISTS fleet_release_epoch_agents;
DROP TABLE IF EXISTS fleet_release_epochs;
DROP TABLE IF EXISTS fleet_release_admission_episodes;
DROP TABLE IF EXISTS fleet_desired_source_idempotency;
DROP TABLE IF EXISTS fleet_desired_source_transitions;
DROP TABLE IF EXISTS fleet_desired_source_states;
DROP TABLE IF EXISTS source_releases;
DROP TABLE IF EXISTS environments;

DROP FUNCTION IF EXISTS _trg_source_releases_sha_immutable();
DROP FUNCTION IF EXISTS _trg_fleet_desired_source_gen_monotonic();
