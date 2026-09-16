# v1.5.0 capabilities: source audit

Captured 2026-09-14. Source candidate: `c7be3a5abb299ba409cad31d469883cd52284969`.
The presentation describes this committed source tree. Its publication links and
this audit are documentation commits layered over that candidate; they do not
claim a self-referential final tag SHA.

The [release documentation audit](../../releases/v1.5.0-audit.md) records a
changed/not-changed decision and source anchor for all 138 pre-existing current
documents, the added release audit, and the open contribution disposition.

## Claim trace

| Slide | Claim | Source |
|---|---|---|
| 1 | Version 1.5.0 and immutable source identity | `src/mac/__init__.py`; candidate commit above |
| 2 | Projects, tasks, dependencies, leases, evidence and separate result boundaries | `src/mac/models.py`, `src/mac/task_lifecycle.py`, `src/mac/task_outcomes.py`, `tests/test_task_outcomes.py` |
| 3 | Preserve selected Hermes profile and verify its upstream service | `deploy/fleet-node-install.sh`; merged PR #804 and #836; `docs/investigations/hermes-deployment-readiness.md` |
| 3 | Remove retired OpenClaw worker-health probes | merged PR #815; `src/mac/worker.py` |
| 3 | Independent managed Python 3.14.7 crash observer | `deploy/fleet-node-install.sh`; merged PR #838; `tests/test_crash_observer.py` |
| 4 | Owned PostgreSQL databases isolate pytest workers and preserve concurrent-run leases | `tests/pg_worker_databases.py`, `tests/conftest.py`; merged PR #835 |
| 5 | No nightly test schedules; full candidate contract, fault, container and documentation checks | `.github/workflows/ci.yml`, `.github/workflows/docs.yml`, `tests/test_deployment_image_artifact.py` |
| 5 | ARM64 smoke builds the candidate locally and publication waits for required checks | `.github/workflows/docs.yml`; no claim that a locally built smoke image is a published or fleet-qualified image |
| 6 | Current replay progresses independently while historical source starves | `tests/fault_replay/reviewer_starvation_probe.py`, `tests/fault_replay/faults.json`, `scripts/fault-replay.py`; historical parent `4407eb4eea99638b2e333ab2641ed84fe0fdd6bc^` |
| 7 | PostgreSQL authority and explicit migrations | `src/mac/store.py`, `src/mac/schema_migrations.py`, `docs/production-deployment.md` |
| 7 | Console is read-only; Workbench prototype and desktop bridge are separate | `observe/tests/readonly.test.ts`, `ide/src/api/mac.ts`, `ide/src/App.tsx`, `desktop/preload.js`, `docs/dashboard-connection.md` |
| 7 | Historical evidence and proposed ADRs are not current capability guarantees | dated records and status fields in the release documentation audit |
| 8 | Artifact publication is separate from fleet cutover | `docs/synchronized-fleet-cutover.md`, `docs/image-publication-and-qualification.md` |
| 8 | SoulGraph, Mission Control and automatic hold release deferred | PR #814, #677 and #782 dispositions in the release audit |
| 8 | Focused Hermes compatibility passes do not imply a green upstream full suite | `docs/python-baseline.md`; ledger task `task_dbc0cad0381a4f33824bcb7e873b5f24` |

## Verification boundary

The release task retains exact-source Linux OpenShell test and packaging logs.
The deck makes no new live fleet-readiness, native desktop acceptance, external
provider performance, or full upstream Hermes-suite claim. No production
configuration or runtime activation is performed by publishing this deck.

The editable eight-slide deck was rendered and every slide inspected for
clipping and overlap. Google publication is verified by exporting the converted
presentation and checking its eight-page count. Only text source is committed;
rendered slide PNGs and the PPTX are disposable build outputs.

Google rejected public sharing with `publishOutNotPermitted`. The Slides copy
therefore remains account-restricted. The public release attaches the original
locally generated PPTX; it does not depend on access to that Google account.
