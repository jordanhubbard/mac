# Dream-cycle output quality

Assessment date: 2026-11-03

## Finding

The hourly jobs identified as `adf20933eff0` (dream cycle) and `2d437df2441e`
(synthesis) cannot produce promotable MAC soul candidates from the current
codebase. The feature they are expected to drive was deliberately removed on
2026-09-30:

- `src/mac/data/postgres/migrations/0004_drop_removed_feature_tables.sql`
  drops `dream_runs` and `dream_candidate_entries` and states that no code
  reads or writes them.
- There is no dream runner, scanner, classifier, synthesis implementation, or
  `LOOKBACK_DAYS` setting in the current source tree.
- `docs/home-consolidation.md` records that the old MAC nap/dream pipeline and
  dream-log importer were removed. It also identifies the remaining
  `~/.hermes/dream_logs/` writer as external Hermes host code, not MAC code.

Consequently, changing `LOOKBACK_DAYS` in this repository would be a false fix.
Any recent files produced under `~/.hermes/dream_logs/` are orphan reports, not
inputs to a current MAC promotion path. The historical field notes further show
that the former classifier frequently promoted single-record keyword matches
at confidence 0.35; those findings were label noise rather than useful source
defects.

## Disposition

Disable the two stale hourly schedules at their external owner rather than
reintroducing the retired pipeline implicitly. If dream synthesis is wanted
again, specify it as a new feature with a durable destination, explicit
promotion gates, and an end-to-end test proving that a representative run
creates at least one reviewed candidate. `LOOKBACK_DAYS=7` belongs in that
owner's configuration and contract, not as an unused MAC setting.

## Evidence limits

The task sandbox contains the canonical repository snapshot but not the gateway-host
host's `~/.hermes/dream_logs/` directory or scheduler definitions. Therefore
this assessment establishes from source why the named schedules cannot promote
MAC candidates; it does not claim to have sampled inaccessible host-local log
files or disabled the external schedules.
