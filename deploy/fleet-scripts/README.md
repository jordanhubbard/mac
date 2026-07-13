# deploy/fleet-scripts

This directory is the **origin-host fleet script archive** for the MAC fleet.

## Purpose

Each origin host in the MAC fleet may maintain local scripts, cron jobs, hooks,
and configuration files outside the main repository. This directory archives
audit records of those host-local assets so that the fleet operator can track
what is running on each host, identify non-standard files, and ensure hygiene.

## mac.fleet_scripts_audit.v1 Schema

Audit records follow the `mac.fleet_scripts_audit.v1` schema. Each record is a
JSON object with the following fields:

| Field               | Type             | Description                                                  |
|---------------------|------------------|--------------------------------------------------------------|
| `schema`            | string           | Always `"mac.fleet_scripts_audit.v1"`                        |
| `host`              | string           | Short hostname of the audited origin host                    |
| `audit_date`        | string (YYYY-MM-DD) | Date the audit was performed                              |
| `hermes_dir`        | string           | Path to the Hermes home directory on the host                |
| `files_present`     | array of strings | Files found in the Hermes directory                          |
| `scripts`           | array            | Local scripts outside the repo worktree                      |
| `cron_jobs`         | array            | Cron entries related to MAC or Hermes                        |
| `hooks`             | array            | Git hooks or event hooks installed on the host               |
| `non_standard_files`| array            | Files that are unexpected or unexplained                     |
| `keepers`           | array            | Files intentionally retained (with justification)            |
| `verdict`           | string           | Summary verdict: `"clean"`, `"needs_review"`, or `"dirty"`  |

## manifest-fragments/

The `manifest-fragments/` subdirectory holds one JSON audit record per host.
Files are named `<hostname>.json` and each conforms to the
`mac.fleet_scripts_audit.v1` schema described above.

These records are written by fleet workers during audit tasks and reviewed by
fleet operators to maintain hygiene across all origin hosts.
