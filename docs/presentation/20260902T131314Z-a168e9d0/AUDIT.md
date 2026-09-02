# Audit — MAC v1.3.5 release candidate

Audited at commit `a168e9d07ca2ae0222ca68de79983075b22e968b` on
2026-09-02T13:13:14Z. This audit supports release `v1.3.5`. Generated CLI and
OpenAPI references are authoritative because CI verifies them against the live
parser and schema.

## Current-document pass

Every current row in `docs/reference/documentation-inventory.md` was reviewed
against the candidate tree. The release range is `v1.3.4..a168e9d0`.

| Documentation surface | Decision | Source anchor |
|---|---|---|
| Generated CLI reference | changed: AgentBus traffic, roll-call, and news output are documented | `docs/reference/cli.md`; PR #710; PR #713 |
| Generated OpenAPI reference | changed: AgentBus and fleet-news read surfaces are documented | `docs/reference/openapi.md`; PR #708; PR #713 |
| Documentation inventory | changed: AgentFabric authoring package is enumerated | `docs/reference/documentation-inventory.md`; PR #694 |
| AgentFabric authoring package | added: presentation authoring materials are supplemental documentation, not MAC runtime behavior | `docs/presentations/agentfabric-overview/`; PR #694 |
| Book chapter 17 | changed: the executable learning/directives example has a 120-second budget after its healthy run exceeded 60 seconds | `docs/book/17-learning-evals-scaling.md`; focused chapter-17 receipt |
| Book chapters 5 and 18 | changed: hub examples wait up to 30 seconds and require a health check before issuing hub commands | `docs/book/05-evidence-review-completion.md`, `docs/book/18-capstone.md`; focused chapter receipts |
| All other current inventory rows | not changed by this range | source review against `v1.3.4..a168e9d0`; no corresponding documentation diff |

Historical archive rows were reviewed as provenance only and are not current
operating contracts. `make docs-check` is required again on the release commit.

## Release claims

| Claim | Source |
|---|---|
| The tag-triggered release workflow builds a wheel, installs it cleanly, verifies task-executor import, and publishes artifacts | `.github/workflows/release.yml`; PR #707 |
| Fleet deployment does not fail solely because a gateway readiness probe was slow while the gateway later proved healthy | `deploy/fleet-node-install.sh`, `deploy/deploy-mac-fleet.sh`; PR #709 |
| Named agents can inspect AgentBus traffic and obtain a roll-call through the CLI and hub dispatch wrapper | `src/mac/cli.py`, `src/mac/dispatch.py`, `src/mac/services.py`; PR #710 |
| Fleet news is available through read-only API, CLI, and observability surfaces with automation-safe output | `src/mac/news_feed.py`, `src/mac/api.py`, `observe/src/views/News.tsx`; PR #713 |
| Contract-test terminal guidance allows up to 3,600 seconds for the full suite instead of treating a healthy long run as hung | `skills/mac-agent-terminal-timeout/SKILL.md`; PR #715 |
| The executable documentation gate permits the healthy learning/directives example to complete within 120 seconds | `docs/book/17-learning-evals-scaling.md`; focused chapter-17 receipt |
| Hub-backed executable book chapters wait for their local authority before continuing, removing a startup-race failure | `docs/book/05-evidence-review-completion.md`, `docs/book/18-capstone.md`; focused chapter receipts |
| `make release` requires release-specific documentation and a rebuildable deck before version bump, gated PR, tag, artifact workflow, and optional fleet deployment | `scripts/release.sh`, `Makefile`, `tests/test_release_workflow.py`; PR #716 |

## Current measured surface

| Claim | Evidence |
|---|---|
| 433 HTTP routes | generated `docs/reference/openapi.md` at `a168e9d0` |
| Generated CLI reference has six top-level command groups | generated `docs/reference/cli.md` at `a168e9d0` |
| 18 executable book chapters | `mkdocs.yml`; `make docs-check` |
| Eight commits since v1.3.4 | `git rev-list --count v1.3.4..a168e9d0` |
| No ADR is marked Proposed in this candidate tree | status-line scan of `docs/adr/` at `a168e9d0` |

## Gates and scope

- Candidate commit: `a168e9d07ca2ae0222ca68de79983075b22e968b`.
- PR #716 CI and Documentation workflows: success, including the expanded sanity scope.
- Release execution must rerun `make lint`, `make test`, and `make docs-check`
  on the final version-bump commit.
- Fleet cutover and image qualification are verified during the optional
  deployment step; a failed newest generation is retained for fix-forward
  diagnosis rather than automatically rolled back.
