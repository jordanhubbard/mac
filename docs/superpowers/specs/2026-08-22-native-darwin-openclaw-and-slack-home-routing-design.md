# Native Darwin OpenClaw and Slack home-channel routing — design

> Status: approved in chat on 2026-08-22. Next step: implementation plan.

## Problem

MAC currently carries two contradictory OpenClaw contracts on Darwin:

- `deploy/fleet-node-install.sh::install_darwin_openclaw_service()` installs a
  user LaunchAgent, calls the stock OpenClaw installer's
  `prepare`/`verify`/`finalize` lifecycle, and registers `withdraw` as its
  rollback hook.
- `deploy/openclaw/install-openclaw-gateway.sh` resolves OpenClaw only inside
  the Linux OpenShell image at `/usr/local/bin/openclaw`. Darwin correctly has
  no managed OpenShell runtime under ADR 0015, so the launchd route has no CLI
  it can execute.

The result is not an intentional unsupported platform: it is a live deployment
route whose implementation cannot succeed.

Slack routing has a related configuration-integrity gap. The fleet already
contains account and home-channel data, and some administrative surfaces route
to it, but the declaration is not a required per-account invariant and the
delivery paths do not share one classification boundary. An instance can join
a Slack workspace without proving that administrative output has a safe
destination. Conversely, the continuity plugin currently mirrors model-
generated conversation summaries into the home channel, which mixes
conversational and administrative traffic.

## Decisions

1. **Darwin runs OpenClaw natively on the host.** OpenShell remains Linux-only.
   The presence of a native chat gateway does not change a Darwin worker's
   `macos_host` execution posture.
2. **MAC owns a rootless native runtime.** It lives below
   `$MAC_OPENCLAW_HOST_DIR/native` (default
   `~/.mac/openclaw/native`), not in Homebrew, `/usr/local`, or an ambient npm
   prefix.
3. **Native and sandbox execution share one lifecycle.** Platform-specific
   command and path adapters sit below the existing
   `prepare`/`verify`/`finalize`/`withdraw` interface. Linux behavior remains
   confined through OpenShell.
4. **Each joined Slack account requires its own home-channel declaration.**
   The key is the OpenClaw Slack account name; the value includes workspace
   identity, channel name, and resolved channel ID. Identical channel names in
   different workspaces are distinct destinations.
5. **Administrative and conversational delivery are separate types.**
   Administrative output may go only to the emitting account's declared home
   channel. Conversational output retains its originating account,
   channel, and thread. Neither path falls back to the other.
6. **Missing home-channel resolution fails preparation before mutation.**
   A joined account with no declared and resolved home channel is invalid
   gateway configuration, not a degraded-but-runnable state.

For the current `mac` fleet:

| Slack account | Workspace | Declared home channel |
| --- | --- | --- |
| `omgjkh` | `omgjkh.slack.com` | `#rockyandfriends` |
| `offtera` | `offtera.slack.com` | `#rockyandfriends` |

The channel names are the same; their workspace/channel IDs are not assumed to
be the same.

## Goals

- Make the existing Darwin launchd OpenClaw route operational without
  installing or invoking OpenShell.
- Preserve the transactional launchd cutover and rollback behavior already
  implemented by `fleet-node-install.sh`.
- Give native and sandbox gateways equivalent OpenClaw version, Slack plugin,
  MAC continuity plugin, stuck-session patch, config, migration, cron,
  verification, and exclusivity behavior.
- Make per-account Slack home-channel completeness a deploy-time contract.
- Make it impossible for administrative delivery to leak into a conversation
  channel or for an ordinary conversational reply to be redirected to a home
  channel.

## Non-goals

- No Darwin task-execution sandbox and no change to the `macos_host`
  attestation.
- No Homebrew-owned or global npm-owned OpenClaw installation.
- No invocation of `curl | bash` or another mutable remote installer.
- No use of OpenClaw's own default LaunchAgent. MAC continues to own the
  existing `${OPENCLAW_LAUNCHD_LABEL}` transaction so there is one supervisor
  and one rollback authority.
- No account-name fallback, channel-name-only fallback, first-account fallback,
  or delivery to the originating conversation when administrative routing is
  incomplete.
- No conversational mirroring into the home channel. The existing continuity
  mirror is retired or disabled as part of enforcing the type boundary.

## Native runtime ownership

### Layout

```text
$MAC_OPENCLAW_HOST_DIR/native/
  releases/<runtime-id>/
    node/                       # pinned official Node Darwin archive
    app/                        # npm lockfile-driven OpenClaw installation
    plugins/mac-continuity/     # reviewed repository plugin
    bin/openclaw                # generation-local wrapper
    manifest.json               # versions, hashes, architecture
  staging/<runtime-id>.<pid>/   # private, removed on failure
```

`runtime-id` is derived from the Node version, OpenClaw version, Slack plugin
version, MAC patch revision, architecture, and reviewed dependency-lock hash.
A complete matching release is immutable and reusable. Preparation constructs
new bytes in `staging`, verifies them, and atomically renames the directory into
`releases`; it never edits the active release in place.

The service wrapper generated for one deploy names the exact candidate release.
The existing launchd transaction already snapshots and restores that wrapper,
so rollback returns execution to the prior runtime without swapping a global
symlink. A failed candidate release is inert and can be garbage-collected after
the generation transaction; it is never selected implicitly.

### Supply-chain contract

- Pin a supported Node release for Darwin arm64 and x86_64. Record the official
  archive SHA-256 values in a reviewed repository manifest.
- Pin `openclaw` and `@openclaw/slack` to the same exact
  `2026.6.11` release used by the Linux image.
- Commit the npm dependency lock used by the native runtime. Installation uses
  that lock, a private MAC-owned prefix, and the exact Node/npm executable from
  the candidate release.
- Permit lifecycle scripts only for the reviewed OpenClaw package identity,
  matching upstream's npm 11.16+/12 guidance. A blocked install guard,
  mismatched version, changed lock, or missing executable fails preparation.
- Apply `patch-stuck-session-recovery.py` to the candidate installation. Its
  existing exact-match behavior must fail closed if upstream source differs.
- Copy the repository's `mac-continuity` plugin into the candidate release and
  install the pinned Slack plugin before candidate verification.
- Never execute the network-fetched upstream installer script.

## Platform adapter

`install-openclaw-gateway.sh` keeps its public subcommands. Internal helpers
resolve a platform contract once:

| Concern | Linux | Darwin |
| --- | --- | --- |
| CLI | `/usr/local/bin/openclaw` in sandbox | candidate native wrapper |
| command execution | `sandbox_command` | bounded direct host command |
| config path | `/home/sandbox/.config/mac-openclaw/openclaw.json` | `$MANAGED_DIR/openclaw.json` |
| runtime env | sandbox-uploaded private file | owner-only host file |
| state | `/sandbox/state` | `$STATE_DIR` |
| workspace | `/sandbox/workspace` | `$WORKSPACE_DIR` |
| continuity plugin | image path | candidate release path |
| cron-plan CLI | sandbox path | candidate native wrapper |

No caller should branch ad hoc on `uname`. Config rendering, wrappers,
verification, cron application, canaries, and rollback consume the resolved
platform contract.

### `prepare`

1. Source and validate host configuration.
2. Resolve Slack accounts and all per-account home channels.
3. Fail before directory/service mutation if any joined account lacks exactly
   one workspace-bound resolved home-channel destination.
4. On Darwin, build or reuse the immutable candidate native release.
5. Render host-native config and owner-only runtime environment.
6. Run continuity migration and host script-job relocation.
7. Write gateway/message/agent wrappers using the exact candidate runtime.
8. Validate config and plugin state with bounded direct native commands.
9. Leave service mutation to `install_darwin_openclaw_service()`, as today.

### `verify`

Darwin verification directly invokes the candidate runtime for config
validation, plugin inspection, health, channel status, semantic identity, and
optional live canaries. It emits the same pending verification record shape as
Linux, extended with:

```json
{
  "execution_posture": "darwin_native",
  "native_runtime_id": "<runtime-id>",
  "native_runtime_manifest_sha256": "<sha256>"
}
```

It must not claim sandbox confinement.

### `finalize`

Finalize consumes the pending verification record, proves the launchd service
is the selected OpenClaw gateway, proves legacy Hermes/NemoClaw supervisors are
inactive, writes the service advertisement, and finalizes cron/script-job state.
The advertisement reports `darwin_native`, not an OpenShell sandbox identity.

### `withdraw`

Withdraw remains safe during launchd compensation:

- stop/unregister only the candidate gateway ownership;
- remove pending verification/advertisement state that belongs to the candidate;
- leave the immutable runtime release available for diagnosis and bounded later
  cleanup;
- never require OpenShell;
- remain idempotent when preparation failed before a service started.

## Slack home-channel declaration

The canonical rendered structure is account-keyed:

```json
{
  "schema": "mac.openclaw_slack_home_channels.v2",
  "accounts": {
    "omgjkh": {
      "workspace_domain": "omgjkh.slack.com",
      "workspace_id": "T...",
      "channel_name": "rockyandfriends",
      "channel_id": "C..."
    },
    "offtera": {
      "workspace_domain": "offtera.slack.com",
      "workspace_id": "T...",
      "channel_name": "rockyandfriends",
      "channel_id": "C..."
    }
  }
}
```

The secret-bearing Slack account file remains separate. The home-channel file
contains route identity, not tokens. Preparation joins the two by exact account
name and verifies:

- every enabled/joined Slack account has one entry;
- no entry names an unknown account;
- workspace IDs/domains agree with the account's authenticated workspace;
- channel name is normalized without `#`;
- channel ID is non-empty and belongs to that workspace;
- duplicate channel names across workspaces remain separate records.

Legacy list-shaped home-channel input may be read only for migration. The
installer writes v2 before starting the new service; runtime delivery reads v2.

## Delivery type boundary

Introduce an explicit delivery classification at the shared routing boundary:

- `administrative`: task claimed/started/progress/completed/failed, executor or
  gateway crashes, degraded-service reports, script-job failures, cron failures,
  deployment/rollback notices, and other machine status.
- `conversational`: a reply to a human or fleet conversation with an origin
  account/channel/thread.

Administrative delivery requires an emitting Slack account name and resolves
only through that account's v2 home-channel entry. It sends to the resolved
channel ID and does not carry an originating conversation target.

Conversational delivery requires its original account/channel/thread and never
consults the home-channel map.

The following existing surfaces must use the shared classification:

- worker task-status sink;
- control-plane task notifier;
- OpenClaw gateway crash/degraded reporting;
- host script-job runner success/failure reporting;
- continuity plugin machine-generated administrative deliveries.

`mirrorExchangeToHomeChannel` is conversational mirroring and conflicts with
the approved boundary. It is removed/disabled; conversation remains where it
originated.

## Error handling

| Condition | Behavior |
| --- | --- |
| unsupported Darwin architecture | fail before service mutation |
| Node archive hash mismatch | delete staging; fail before service mutation |
| npm lock/package/version mismatch | delete staging; fail before service mutation |
| upstream patch no longer applies exactly | delete staging; fail before service mutation |
| native candidate command/config/plugin verification fails | retain bounded diagnostics; fail before launchd mutation |
| joined Slack account lacks home declaration | fail before runtime installation/service mutation |
| declaration workspace differs from authenticated account | fail before mutation |
| administrative send lacks emitting account | reject; do not infer a default |
| administrative home delivery fails | record classified delivery failure; do not retry into a conversation |
| conversational send lacks origin | reject as malformed conversation; do not use home channel |
| rollback after candidate start | restore prior wrapper/plist and invoke idempotent native `withdraw` |

## Tests

All behavior is implemented test-first.

### Native runtime

- Darwin `prepare` never calls `find_openshell`, `sandbox_command`, Docker, or
  an image build.
- Darwin arm64/x86_64 select the exact reviewed Node asset and reject unknown
  architectures.
- Hash mismatch, incomplete npm install, wrong OpenClaw version, blocked
  lifecycle guard, Slack plugin mismatch, and patch drift each fail before
  launchd mutation.
- Re-running prepare reuses an identical complete immutable release.
- A changed runtime input creates a new release without modifying the prior one.
- Generated gateway/message/agent wrappers name the exact native release and
  host paths.
- Native verify emits `darwin_native`, validates config/plugins/health, and
  never reports OpenShell confinement.
- Native finalize and withdraw are idempotent.
- The existing launchd transaction restores the prior generation when native
  verification or finalization fails.
- Existing Linux/OpenShell tests remain unchanged and green.

### Slack routing

- `omgjkh` and `offtera` both resolve `#rockyandfriends` to distinct
  workspace-bound channel IDs.
- Any enabled account missing from the home-channel map fails preparation.
- Unknown account, wrong workspace, missing channel ID, and duplicate account
  declarations fail validation.
- Task progress, task failure, crash, degraded gateway, and script-job failure
  each route only to the emitting account's home channel.
- Administrative delivery never uses an originating conversation channel as
  fallback.
- Ordinary channel and threaded replies preserve account/channel/thread and do
  not consult the home-channel map.
- Conversational mirroring into the home channel no longer occurs.

## Documentation

- Amend ADR 0015: OpenShell execution remains Linux-only, while OpenClaw is a
  native Darwin system service managed by MAC's rootless runtime and launchd
  transaction.
- Update production deployment and onboarding docs with the native runtime
  location, supply-chain verification, per-account home-channel requirement,
  two-workspace `#rockyandfriends` configuration, and diagnostic commands.
- Update the OpenClaw runbook for native prepare/verify/finalize/withdraw and
  rollback evidence.

## Rollout

1. Merge code and contract tests without deploying.
2. Deploy to the Darwin hub node while fleet dispatch remains held.
3. Verify native runtime manifest, launchd service, OpenClaw health, Slack
   plugin, account/workspace identity, and both home-channel resolutions.
4. Send one administrative canary through each Slack account and prove each
   lands only in that workspace's `#rockyandfriends`.
5. Send one conversational threaded canary in a non-home channel and prove the
   reply remains in that thread with no home-channel mirror.
6. Exercise rollback to the prior generation before releasing dispatch.
7. Release fleet work only after the canaries and rollback proof pass.
