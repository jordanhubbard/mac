# Audit — MAC v1.3.4 capabilities deck

Audited at commit `e78a7ba7` on 2026-08-31T14:37:51Z. Generated CLI and OpenAPI references are authoritative because CI verifies them against the live parser and schema.

## Current-document pass

Every row of `docs/reference/documentation-inventory.md` was reviewed against the candidate tree. The v1.3.3 to v1.3.4 range changes five documentation files:

| File | Decision | Source anchor |
|---|---|---|
| `docs/repository-runtime-contract.md` | changed: Git 2.38 floor is now declared | `.mac/project.yaml`; PR #691 |
| `docs/env-config-reference.md` | changed: contract Git selection variables | generated registry; PR #697 |
| `docs/archive/field-notes/investigation-contract-gate-env-8454149d-resolution.md` | historical resolution added | PR #700 |
| `docs/archive/index.md` | generated archive index changed | PR #700 |
| `docs/reference/documentation-inventory.md` | generated inventory changed | PR #700 |

All other current inventory rows are not changed by the release range. The field note and archive index are historical, not current behavior. `make docs-check` passed on the candidate.

## Release claims

| Claim | Source |
|---|---|
| Contract tests now fail fast when Git 2.38 or newer is unavailable | `scripts/run-contract-tests.sh`; PR #697 |
| Fleet and runner provisioning declare and enforce Git 2.38 or newer | `.mac/project.yaml`, container definitions, fleet installer; PR #691 |
| Failed test runs label coverage partial instead of presenting a complete safety measurement | `scripts/coverage-policy.py`; PR #696 |
| The preferred sanity gate provisions PostgreSQL before impact-map collection | `scripts/run-sanity-tests.sh`; PR #695 |
| macOS docs CI uses supported Homebrew PostgreSQL 17 | `.github/workflows/docs.yml`; PR #701 |
| Report execution survives replacement of a host Python interpreter | `src/mac/worker.py`, `src/mac/deploy_env.py`; PR #703 |
| Lease telemetry tolerates bounded hub/database clock skew without accepting old events | `src/mac/services.py`; PR #704 |
| Ruff format gate was restored before release | PR #702 |

## Current measured surface

| Claim | Evidence |
|---|---|
| 430 HTTP routes | generated `docs/reference/openapi.md` |
| 125 CLI verbs | generated `docs/reference/cli.md`: task 45, project 9, agent 17, admin 54 |
| 18 executable book chapters | `mkdocs.yml`; `make docs-check` |
| 10 commits since v1.3.3 | `git rev-list --count v1.3.3..e78a7ba7` |
| 10 Proposed ADRs | status lines in `docs/adr/` |

Ledger census at 2026-08-31T14:37Z: blocked 377, cancelled 3,573, completed 745, failed 2,172, needs_input 22, needs_review 1, open 143, reviewing 19, stopped 5, waiting 29. Counts are observations, not product guarantees.

## Gates

- Candidate commit: `e78a7ba706c7b8e35311ddc2fecdd3cc488b97fa`.
- GitHub Documentation workflow on the candidate: success.
- GitHub CI workflow on the candidate: success.
- Local `make docs-check`: success.
- Local `make lint` was red on the prior tree, repaired by PR #702, and clean afterward.
- The prior local full suite exposed two deterministic lease-telemetry failures, repaired by PR #704. PR and post-merge CI are green on this candidate.

## Release

This audit supports v1.3.4. Fleet cutover and image qualification are outside this release artifact.
