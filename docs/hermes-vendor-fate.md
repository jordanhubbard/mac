# Fate of the vendored Hermes tree

**Verdict: removed.** `src/mac/_hermes` (~444k lines) was deleted in PR #377
on 2026-08-17. Hermes is the only human interface and chat gateway, installed
separately. The in-tree Hermes snapshot was
inactive and larger than mac's own code. This note records the four
pre-deletion checks and the post-removal inventory so the decision stays
auditable during the port.

## Pre-deletion checks (a)–(d)

Measured against prepared tip `7f2850f76361d676405cacd0491fd017f6f5f5c3`
(tree already absent).

| Check | Answer | Evidence |
| --- | --- | --- |
| **(a)** Does anything import `hermes_cli` at runtime? | **No** | Exact search for `from hermes_cli` / `import hermes_cli` under `src/mac/` finds zero live import sites. `mac.hermes_config_surface._hermes_config_module` raises `ModuleNotFoundError` by design (“vendored hermes_cli was removed”). |
| **(b)** Do OpenShell sandbox images or the chat gateway invoke a vendored Hermes entry point as a subprocess? | **No** | `mac.agent_command` has no executable `hermes_cli.main` string constants (comment-only history). Task-executor / OpenShell tests assert `hermes_cli.main` is absent from coding-agent argv. `deploy/openshell/mac-hermes.Containerfile` installs mac only and has no `zz_hermes_vendor.pth` injection (stale narrative comments may still mention the old hook; they are not load-bearing). |
| **(c)** Are vendored plugins/skills (`src/mac/_hermes/plugins`, `.../skills`) loaded by the active gateway path? | **No** | Those directories no longer exist. The external Hermes runtime reads its own *home* workspace, not `src/mac/_hermes`. |
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

## Update (2026-09-13): bounded external compatibility patch

The shared Python 3.14.7 baseline exposes an incompatibility in the externally
installed Hermes daemon thread pool. `deploy/hermes/python314.patch` records
the repair and test-harness corrections against one upstream commit;
`python314-source.json` pins that commit, the patch checksum, and every affected
file's original and patched hashes. It preserves the locked dependency versions.
Apply it to a separate external staging checkout, never a serving checkout.

`deploy/hermes/runtime-context.patch` is the independently pinned prompt
integration for the same upstream revision. It extends Hermes's supported
context-file builder so the deployment-owned MAC runtime markdown is additive
to workspace instructions and `SOUL.md`; it does not replace either one or
change `terminal.cwd` discovery.

`deploy/hermes/cron-routing.patch` makes an explicit Slack channel destination
post at the channel root. A job's creation thread remains provenance and no
longer changes that explicit destination. `deliver=origin` retains origin routing,
and an explicit `slack:channel:thread` destination retains its requested thread.
The `attach_to_session` setting controls transcript mirroring independently.
Existing channel-only jobs acquire this behavior when the qualified runtime is
deployed; jobs that need a thread must name it or use `deliver=origin`.

This accepts a limited patch-maintenance obligation for the requested migration.
Requalify the patch when changing the upstream revision, and retire it once a
qualified upstream release supplies the fixes. It does not restore the snapshot,
an in-process import, an overlay, a re-vendor job, or a container `.pth` injection.
The architecture tests retain those prohibitions and check external patch/manifest
integrity instead of forbidding every file with a `.patch` suffix.

## Qualified external releases

The gateway installer prepares an external release from the reviewed revision,
applies the reviewed manifests, and installs the locked `slack` and `mcp` extras using
the reviewed Python and uv versions. Hermes itself is installed in editable
mode, as supported by upstream, so service modules resolve from the selected
environment even when the working directory is the profile. Qualification runs
the actual service stderr wrapper and CLI child with `--help` from that directory,
without adding the source directory to `PYTHONPATH` or `sys.path`. The profile's
runtime context and persona must appear in the candidate's constructed prompt
before selection.

The regular `~/.local/bin/hermes` launcher is the runtime selection point.
Deployment replaces it atomically after qualification and uses upstream's CLI
to install the service. Verification checks the release's recorded source and
package versions, the launcher, and the service's interpreter/profile before
accepting live messaging readiness. Startup health resolves that same launcher
instead of trusting a stale `MAC_HERMES_AGENT_DIR` override. Repeated deployment
reuses a valid qualified release without synchronizing the serving environment.

Releases live under `~/.mac/hermes-runtimes/`, separate from profile data.
The existing fleet transaction snapshots the launcher and actual upstream
service definitions before activation. Its hold and recovery policy controls
failures after selection; no second deployment controller is added. Failed
candidates are retained for diagnosis. Required integrations belong in the
reviewed release recipe; arbitrary untracked source modifications and incidental
packages from old runtimes are not automatically copied. Live canary evidence
remains required before releasing dependent work.

The [ownership investigation](investigations/hermes-runtime-ownership.md)
records the evidence, corrected diagnosis, and remaining rollout proof.

## Update (2026-09-05): Hermes runs as an external install, still not vendored

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
the service, `hermes send` for one-off/cron message delivery, and `hermes -z
<prompt>` for one-shot agent turns.

## Update (2026-09-05): repo-owned lifecycle automation restored

`deploy/hermes/install-hermes-gateway.sh` codifies the host lifecycle with a
`prepare`/`verify`/`finalize`/`withdraw` shape (no container lifecycle, since
Hermes runs as a bare host process rather than an OpenShell sandbox): run
upstream's shell installer if `hermes` isn't already on `PATH`, set the
channel-behavior policy from fleet config (`slack.require_mention` /
`slack.free_response_channels`, resolved from the `hermes.slack_home_channel_name`
fleet-config field to a channel id via Hermes's own cached channel directory),
and install the gateway as a properly supervised background service
(`hermes gateway install`). It only shells out to the externally installed
`hermes` CLI; it does not touch the deleted vendored-in-process model.

## Update (2026-10-01): fleet orchestration deleted

`deploy/deploy-mac-fleet.sh` and `deploy/fleet-node-install.sh` have been
deleted; hosts are updated with `scripts/fleet-update` (see
[Updating the fleet with fleet-update](operations/fleet-update.md)). A new
node's Hermes gateway is again prepared by running
`deploy/hermes/install-hermes-gateway.sh prepare` on it, as the "Provision a new
host" checklist says; `scripts/fleet-update --hermes` restarts it on update.
