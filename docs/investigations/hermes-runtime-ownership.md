# Hermes deployment ownership investigation

Status: candidate implementation under verification; not deployed. Task: `task_163c2fde94c445e7b98fac05797ffe14`.

## Evidence and corrections

Reviewed the 100 first-parent commits ending at `19729643` and the related
Hermes implementation history. The recurring failures cross configuration,
runtime selection, readiness, and rollback boundaries:

| Change | What it established | Remaining boundary |
| --- | --- | --- |
| #753, #755 | External Hermes CLI lifecycle calls replaced deleted in-process wrappers | An installed command was accepted without qualifying its source or dependencies |
| #756, #757, #759, #765 | Model fields, Slack admission, channel configuration, gateway selection | Standalone setup and fleet setup had different configuration responsibilities |
| #804 | Preserve the installed service's Hermes profile | Profile discovery remained distinct from runtime discovery |
| #816, #817, #831 | Reviewed Python baseline and dependency installation; external compatibility patch | Hermes patch application remained a separate operator action |
| #836 | Bind messaging readiness to the current process | Messaging liveness did not prove prompt integration |
| #844 | Reviewed prompt patch, patch-set validation, constructed-prompt checks | `prepare` never called `qualify-stage`; qualification did not select or activate a runtime |
| #847 | Split Linux verifier creation, upload, bootstrap and execution | This change did not own Hermes activation |

The failed hub deployment at 2026-09-15 14:49 UTC reported
`active Hermes prompt omitted MAC runtime context` after restarting launchd.
That message proves the constructed-prompt assertion failed. It does **not**
identify the underlying cause as missing source, missing configuration,
truncation, or a different runtime. The initial response incorrectly presented
missing patch source as established fact.

At 18:22 UTC the live runtime's prompt-builder SHA-256 was
`b90968227468fd6d84411f9abe8eaf2317cbe5305e878ef152ed8dbccb6bf8c7`, exactly the
reviewed patched output. A direct probe constructed the prompt with all 8,822
characters of the runtime markdown, and the installer finalizer confirmed
current-writer Slack connectivity. This does not reproduce the earlier failure:
the source file had changed at 15:20 UTC, context/configuration changed around
15:28 UTC, and deployment `20260915T152214Z` completed afterward. The agent
snapshot showed the hub and both Linux workers healthy, idle, and unheld. Attribution
of that intervening repair has not been established.

No runtime manager or fleet mutation should be justified by treating these
different observations as one unchanged system. Historical source evidence is
needed to identify the exact original failure. The reproducibility gap below is
independently demonstrated by current repository code.

## Fundamental defect

MAC requires specific upstream source plus two reviewed patches, but its
installer's desired state is merely that a `hermes` executable exists.
`install_hermes` returns immediately when it finds that command. `prepare`
configures and restarts it without qualifying source or dependencies.
`qualify-stage` is available only as a separate manual operation.

Runtime identity is then independently derived from launcher text in the
installer, environment variables in startup health, and absolute interpreter
paths written by the upstream service installer. The profile has its own
service-definition resolver. These observations are useful individually, but
there is no deployment-owned selection tying them to one qualified candidate.
A fleet assembled by manual intervention can pass while a fresh installation
or subsequent replacement cannot reproduce it.

There is already a substantial deployment transaction with generation fencing,
phase-one quiescence, sealed rollback intent, configuration snapshots and
startup attestation. Saying the system has no rollback contract was too broad.
Any runtime selection change must participate in that transaction and its
existing recovery policy. A second independent deployment state machine would
add another competing owner.

## Corrective boundary

1. Define Hermes desired runtime from the existing reviewed upstream revision,
   both patch manifests, Python/uv baseline and explicitly selected dependency
   extras. Slack and MCP requirements must be declared; retaining incidental
   packages with `uv sync --inexact` alone cannot make a fresh install repeatable.
2. Prepare source and its own environment outside the active profile. Apply the
   reviewed patch set and synchronize the selected locked dependencies before
   touching the serving runtime. Build the environment at its final path;
   virtualenv executables can embed absolute paths.
3. Qualify the candidate using the actual prompt builder and selected profile
   context, with imports for required integrations. Publish a record of the
   exact qualified inputs. Do not infer qualification from a directory name.
4. Bind candidate selection to the existing deployment generation. Capture the
   previous selection, CLI launcher and service definition in the existing
   rollback plan before activation. Leave user profile data under its present
   ownership; runtime replacement is not profile migration.
5. Use one selected runtime for the CLI, service installation and health probe.
   The service may contain concrete paths, but verification must compare them
   with the selected runtime. Do not resolve a virtualenv's interpreter symlink
   to its base Python and lose its installed packages.
6. Prove the selected runtime and current service process agree, then apply the
   existing commit/release rules. A failure remains held or restores the prior
   generation according to the controller's policy. Do not automatically
   restore broad profile snapshots over live conversation data.

This should remove duplicate runtime-selection logic as consumers move to the
shared selection. Upstream still owns Hermes's CLI/service mechanics. MAC owns
the exact external runtime it has qualified for its deployment requirements.

## Required proof before completion

- Fresh install and upgrade both activate the selected patched runtime with
  Python 3.14.7 and the required locked Slack/MCP dependencies.
- Qualification failure leaves the previous launcher and service untouched.
- Activation failure and interruption are recoverable through the existing
  transaction without mismatching the selected runtime and service definition.
- Repeating deployment with the same inputs is idempotent; a stale environment
  override cannot make health inspect a different runtime.
- Both Linux service and macOS launchd paths are covered by meaningful lifecycle
  tests; runtime tests execute on Linux within the established isolation policy.
- Repository quality gates pass, code is pushed and landed, and a deployment
  using the integrated path passes live prompt and messaging readiness checks.
- A canary completes through the normal workflow before dependent work is
  released. Existing healthy fleet state alone does not prove the new path.

The deployed fleet is currently recovered, but this reproducibility work is
still open. This investigation is not completion evidence for the fix.

## Candidate verification

The interactive Codex session is implementing this task in its isolated worktree;
keep the task held against automated dispatch. Its registered participant is
`agent_codex_trust_implementation`. Manual claim was rejected because the
participant is dispatch-held; this note records the owner without making an
interactive session eligible for automatic assignment.

The new release preparation path has passed fresh preparation against the actual
pinned upstream repository inside Linux OpenShell: reviewed patches applied,
locked Slack/MCP dependencies installed, the actual prompt builder preserved MAC
and persona context, the CLI executed, and a repeat preparation reused the same
qualified release. Earlier focused lifecycle checks passed 99 tests.

The full contract gate remains outstanding. Its preflight found a stale generated
documentation inventory and fleet-specific names in this investigation; both
were corrected. Review also caught a misplaced parent-runtime refresh in the
uncommitted candidate. It is now in the Hermes preparation function with a
behavioral regression test covering the parent/child process boundary.

These are candidate checks, not fleet rollout or end-to-end canary evidence.
