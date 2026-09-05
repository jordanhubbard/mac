# Fate of the vendored Hermes tree

**Verdict: removed.** `src/mac/_hermes` (~444k lines) was deleted in PR #377
on 2026-08-17. OpenClaw is the live gateway; the in-tree Hermes snapshot was
inactive and larger than mac's own code. This note records the four
pre-deletion checks and the post-removal inventory so the decision stays
auditable during the port.

## Pre-deletion checks (a)–(d)

Measured against prepared tip `7f2850f76361d676405cacd0491fd017f6f5f5c3`
(tree already absent) and the live-fleet evidence cited in
`docs/hermes-retirement-premises.md`.

| Check | Answer | Evidence |
| --- | --- | --- |
| **(a)** Does anything import `hermes_cli` at runtime? | **No** | Exact search for `from hermes_cli` / `import hermes_cli` under `src/mac/` finds zero live import sites. `mac.hermes_config_surface._hermes_config_module` raises `ModuleNotFoundError` by design (“vendored hermes_cli was removed”). |
| **(b)** Do OpenShell sandbox images or the openclaw gateway invoke a vendored Hermes entry point as a subprocess? | **No** | `mac.agent_command` has no executable `hermes_cli.main` string constants (comment-only history). Task-executor / OpenShell tests assert `hermes_cli.main` is absent from coding-agent argv. `deploy/openshell/mac-hermes.Containerfile` installs mac only and has no `zz_hermes_vendor.pth` injection (stale narrative comments may still mention the old hook; they are not load-bearing). |
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

## Update (2026-09-05): Hermes reactivated on the hub, still not vendored

OpenClaw (the gateway that replaced Hermes above) turned out to be unreliable
at a layer this repo does not own: its OpenShell sandbox's state mount runs
on Docker Desktop's overlayfs, where POSIX advisory locking is broken enough
that a fresh, empty SQLite WAL database hangs indefinitely under a trivial
write load. Three OpenClaw-side fixes landed first (cron schedule collision,
a host-side flock mutex, a message-body encoding bug) before this filesystem
problem was isolated as the actual, unfixable-from-here root cause.

The retirement premise above was that Hermes's memory capabilities were
already covered by mac itself — true, but it did not account for OpenClaw's
reliability. The hub's chat gateway was cut back to Hermes as a result.

**This is not a re-vendoring.** The mistake in 2026-08 was carrying a
444k-line patched snapshot in-tree, not depending on Hermes at all. Hermes
does not support a normal `pip install` either — its own `setup.py` refuses
to build a wheel or sdist ("Hermes is distributed via the shell installer,
Docker image, or Nix"). The hub runs it via upstream's own shell installer
(`curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`), which
installs a fully self-contained checkout and venv under `~/.hermes/`,
entirely outside this repo and outside `mac`'s own Python environment. There
is nothing under `src/mac/` importing `hermes_cli`, no patch set, and no
re-vendor job to maintain — the earlier in-process `mac.hermes_gateway`
launcher (which assumed a pip-installed, importable `hermes_cli`) was removed
again for exactly this reason; it never worked against upstream's real
distribution model.

Management is entirely through Hermes's own CLI, installed to
`~/.local/bin/hermes`: `hermes gateway install` / `hermes gateway stop` for
the service, `hermes send` for one-off/cron message delivery, `hermes -z
<prompt>` for one-shot agent turns, and `hermes claw migrate` for pulling an
OpenClaw workspace's identity/memory/skills across (used once, live, to bring
the hub's accumulated OpenClaw state into Hermes during the cutover).
`deploy/openclaw/run-script-cron-job.py`'s two-stage host cron runner
(`mac-cron-script-runner`) now drives its agent turn and delivery through
this CLI instead of the OpenClaw sandbox wrappers; see that file's
`_default_hermes_bin()` / `default_agent_runner()` / `message_args()`.
