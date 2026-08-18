# Fate of the vendored Hermes tree

**Verdict: removed.** `src/mac/_hermes` (~444k lines) was deleted in PR #377
on 2026-08-17. OpenClaw is the live gateway; the in-tree Hermes snapshot was
inactive and larger than mac's own code. This note records the four
pre-deletion checks and the post-removal inventory so the decision stays
auditable during the port.

## Pre-deletion checks (a)–(d)

Measured against prepared tip `27ed8af9491e640226330dd9c9371a3a7de82221`
(tree already absent) and the live-fleet evidence cited in
`docs/hermes-retirement-premises.md`.

| Check | Answer | Evidence |
| --- | --- | --- |
| **(a)** Does anything import `hermes_cli` at runtime? | **No** | Exact search for `from hermes_cli` / `import hermes_cli` under `src/mac/` finds zero live import sites. `mac.hermes_config_surface._hermes_config_module` raises `ModuleNotFoundError` by design (“vendored hermes_cli was removed”). |
| **(b)** Do OpenShell sandbox images or the openclaw gateway invoke a vendored Hermes entry point as a subprocess? | **No** | `mac.agent_command` has no executable `hermes_cli.main` string constants (comment-only history). Task-executor / OpenShell tests assert `hermes_cli.main` is absent from coding-agent argv. `deploy/openshell/mac-hermes.Containerfile` installs mac only and has no `zz_hermes_vendor.pth` injection (stale comments may still mention the old hook; they are not load-bearing). |
| **(c)** Are vendored plugins/skills (`src/mac/_hermes/plugins`, `.../skills`) loaded by the active openclaw path? | **No** | Those directories no longer exist. OpenClaw continuity uses `deploy/openclaw/migrate-hermes-continuity.py` against a Hermes *home* workspace, not `src/mac/_hermes`. |
| **(d)** Does `deploy/hermes/SNAPSHOT.md` describe an obligation that survives removal? | **No** | `deploy/hermes/` (including `SNAPSHOT.md` and re-vendor tooling) is gone. CI has no `hermes-revendor` job; `report-main-red` needs do not list it. |

## What was removed

- `src/mac/_hermes/` — pinned upstream snapshot
- `src/mac/hermes_vendor.py`, `src/mac/hermes_gateway.py`
- `deploy/hermes/` — patches, `SNAPSHOT.md`, vendor scripts
- CI hermes-revendor job and the sandbox `zz_hermes_vendor.pth` injection
- `mac-hermes-gateway` console script / hermes-gateway optional extra

## What remains (intentionally)

- First-party mac modules named `hermes_*` (`hermes_adapter`, `hermes_startup`,
  `hermes_config_surface`, …) — control-plane / migration surfaces, not the
  vendored runtime
- `mac-hermes` console script → `mac.hermes_adapter:main`
- Historical ADRs and field notes that describe the old layout
- Stale narrative comments in a few deploy scripts that still *mention*
  the old in-tree path; they are not load-bearing and do not reintroduce
  the tree
- Optional `MAC_HERMES_AGENT_DIR` — operator override for an *external*
  Hermes checkout; deploy no longer defaults it at the deleted vendor path

## ADR

ADR 0001 is amended to **Superseded (vendoring premise ended 2026-08-17)**.
Hermes can be fetched and patched on demand if needed again; git history
retains the snapshot.
