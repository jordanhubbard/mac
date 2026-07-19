# Synchronized Fleet Cut-over

> **Cut-over gate:** the typed protocol in
> [`fleet-cutover-transaction-protocol.md`](fleet-cutover-transaction-protocol.md)
> is implemented, but production publication remains frozen until its complete
> repository and fault-matrix gates pass. This document is the operator path;
> it does not override those release blockers.

The fleet deployer uses a three-phase epoch: hold and drain the complete
selected cohort, deploy and prove every new generation, then atomically hand
the deployment holds to one successor hold in the hub transaction. No selected
worker becomes dispatchable between deployment and synchronized-pipeline
activation. A worker that cannot join the epoch remains held; the deployer
never degrades an exact cut-over into a partial transition.

Synchronization here is a logical barrier, not a claim that heterogeneous
hosts share a wall-clock start time. Each node may reach a barrier at its own
speed, but no member may enter the next mutation or ownership phase until the
controller has exact, generation-bound evidence from the entire selected set.
Monotonic deadlines bound local waiting without making clock agreement part of
the safety proof.

## Architecture and acceptance invariants

The cut-over is complete only when all of these invariants hold together. A
change that does not advance or protect one of them belongs outside the
cut-over.

| Invariant | Enforcement boundary | Required evidence |
| --- | --- | --- |
| Immutable release | Source commit, certifier image, and worker runtime are exact digest-pinned identities | CI publication receipts and local anonymous-pull verification name the same commit |
| Exact cohort | Stable agent IDs and current targets come from the frozen fleet registry | Selected set and release receipt contain the same IDs exactly once |
| Quiescent boundary | Every selected agent is held, drained, task-free, service-claim-free, and free of old supervisor- or daemon-owned resources before artifact mutation | Hub drain records, bounded supervisor-unload proof, exact OpenShell inventory, cross-runtime container inventory, and a generation-bound local quiescence receipt |
| Recoverable node generation | Source, virtualenv, service definitions, and supervisor topology change as one fenced generation; a failed replacement cannot be accepted as deployable | Prior-state receipt, owner-private artifact snapshots, mutation journal, bounded rollback receipts, successful remote transaction exit, and controller-side post-manifest revalidation |
| Crash-recoverable cohort | The controller persists intent before every mutation and compensates prepared nodes in reverse order after a proved-absent hub commit | Owner-fenced cohort journal, retained restore-contract digests, phase-2 rollback receipts, and exact epoch-status reconciliation |
| Atomic ownership handoff | Deployment holds become one successor hold in a single database transaction | No committed row is unheld; stale ownership rolls back the entire cohort |
| Idempotent epoch | Epoch identity binds holds, outcome, successor reason, generation, credential principal, and report-executor expectations | Same request replays one receipt; any changed input is rejected |
| Attested generation | Every worker proves the deployed generation, bound principal, startup self-test, and read-only report executor | Exact post-deploy rows and controller approval match the immutable runtime |
| Controlled activation | Pipeline and landing stay disabled during deployment; successor holds remain through activation | Runtime health passes before any explicitly selected canary is released |

## 1. Bootstrap an older hub by itself

An older hub may not expose reason-CAS or batch-release routes. Bootstrap only
the configured hub, while it is already operator-held, with the work-package
pipeline disabled:

```bash
MAC_DEPLOY_ALLOW_LEGACY_CAS_BOOTSTRAP=1 \
MAC_DEPLOY_WORK_PACKAGE_PIPELINE_ENABLED=0 \
MAC_DEPLOY_WORK_PACKAGE_LANDING_ENABLED=0 \
MAC_DEPLOY_OPENSHELL=1 \
MAC_DEPLOY_OPENSHELL_ARGS='--enable --fail-closed' \
MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE='ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:<tested-main-digest>' \
deploy/deploy-mac-fleet.sh --hub <hub-agent> <hub-agent>
```

Legacy bootstrap preserves the existing operator hold. It rejects hold
adoption, exact-full-cohort mode, and any non-hub target. Afterward, confirm the
hub OpenAPI document contains all eight routes required by the cohort deployer:

- `/agents/{agent_id}/dispatch-hold/acquire`
- `/agents/{agent_id}/dispatch-hold/release`
- `/agents/dispatch-hold/release-batch`
- `/agents/dispatch-hold/transition-batch`
- `/agents/dispatch-hold/epochs/{epoch_id}`
- `/agents/{agent_id}/attestation-key/recover`
- `/agents/{agent_id}/report-repository-executor/approve`
- `/agents/{agent_id}/report-repository-executor/revoke`

## 2. Authorize exact adoption of existing holds

The deployer does not infer permission to clear an operator hold. If selected
agents are already held, create a one-invocation authority file from freshly
read live rows. Use stable `agent_...` IDs and copy each reason exactly; never
put credentials or authenticated URLs in this file.

```json
{
  "schema": "mac.dispatch_hold_adoptions.v1",
  "fleet": "<fleet-name>",
  "hub_agent": "<hub-agent-name>",
  "source_commit": "<exact-tested-main-commit>",
  "adoptions": [
    {
      "agent": "agent_<stable-id>",
      "reason": "<exact-current-dispatch-hold-reason>"
    }
  ]
}
```

The file must be a regular, non-symlink file owned by the invoking user, be
owner-readable, have no group/other permission bits, and be at most 1 MiB:

```bash
chmod 0600 /path/to/hold-adoptions.json
```

The deployer opens it without following a final symlink, validates duplicate
keys and exact schema, and snapshots it once into the deployment's private
temporary directory. It rejects a wrong commit, hub, fleet, unselected agent,
duplicate agent, unheld agent, or reason drift.

## 3. Deploy the complete cohort

Resolve every target again from `~/.mac/fleets.yaml`, then name every selected
agent explicitly. Supplying `--hold-adoptions` automatically enables
`--require-release-all-selected`:

```bash
MAC_DEPLOY_WORK_PACKAGE_PIPELINE_ENABLED=0 \
MAC_DEPLOY_WORK_PACKAGE_LANDING_ENABLED=0 \
MAC_DEPLOY_OPENSHELL=1 \
MAC_DEPLOY_OPENSHELL_ARGS='--enable --fail-closed' \
MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE='ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:<tested-main-digest>' \
deploy/deploy-mac-fleet.sh \
  --hub <hub-agent> \
  --hold-adoptions /path/to/hold-adoptions.json \
  --successor-hold-reason 'synchronized pipeline activation refreeze <exact-tested-main-commit>' \
  <hub-agent> <agent-2> <agent-3> <agent-4> <agent-5>
```

Before hub epoch open or service mutation, the deployer resolves each route
from the frozen `~/.mac/fleets.yaml`, binds its live endpoint identity, arms the
generation-specific phase-1 restore contract, and obtains a read-only
`mac.fleet_prerequisite_bundle.v1`. The bundle proves the selected supervisor,
required executables, owner-private input files, source and immutable image
identity, and required loopback service readiness. Any missing prerequisite
fails the complete cohort before the hub is changed or any service is stopped.
Prerequisite repair and package or image installation are separate operations;
the synchronized transaction never performs them opportunistically.

The hub then opens one typed epoch that atomically validates the selected rows,
exact prior holds, absence of active work, staged worker principals, candidate
attestation keys, report-executor intent, and policy revision. The open receipt
contains only identities and digests. Pending worker credentials and candidate
keys remain staged alongside the current active identities until commit.

The controller also creates an owner-private durable cohort journal before the
first remote mutation. The journal binds the ordered stable-agent set, exact
source and runtime identities, deterministic per-agent generations, controller
nonce, and every state transition. A phase-1 transition is accepted only with
the digest of that node's retained restore contract; a phase-2 transition is
accepted only after its exact generation-bound rollback contract is durable.
Journal updates use compare-and-swap transitions and atomic, fsync-backed
owner-only files. The journal contains no targets or credential material.

Controller exit is therefore an abort boundary, not permission to abandon a
partly changed cohort. A live controller owns the journal exclusively. A later
invocation may adopt it only after proving the recorded controller is gone,
then it restores in reverse mutation order. Recovery invokes the exact retained
`restore_executable.argv` recorded and digested before phase 1, never a helper
from the current checkout; nodes that reached phase 2 use their exact recorded
generation rollback first. Each successful compensation is journaled so that
another interruption resumes rather than repeats ambiguous work.

The hub handoff has a distinct uncertainty rule. The controller durably records
`commit-start` before calling the atomic epoch endpoint. If the response is
lost, recovery queries the epoch by its exact identity digest. A matching
`committed` result forces roll-forward; a matching `proved` result replays the
same commit. Neither permits node rollback. An identity mismatch, transport
failure, or unknown response is a hard stop.

Open and prove requests are stored as owner-private, one-use replay envelopes
on the hub before their journal intent is advanced. The durable cohort journal
contains only their secret-free plans and digests. Recovery can therefore
resolve a lost response without reconstructing credentials from ambient files,
and removes each envelope after its receipt is journaled or the epoch aborts.

After every selected hub row is held and drained, the controller invokes the
same phase-1 helper on every selected node. The helper stops and proves absent
the worker plus Hermes, OpenClaw, and legacy Nemo gateway identities through
the node's exact supervisor; supervisord nodes inspect every usable distinct
manager instance and deduplicate aliases by manager PID. The stop set
deliberately excludes the control plane. The helper then runs the reviewed
daemon-resource block before any source or image is replaced. Each node
atomically publishes an owner-only
`mac.phase1_cohort_quiescence.v1` receipt that binds the supervisor proof, the
`pre_source` daemon receipt, the exact reviewed function-block digest, source
revision, and deployment generation. Phase 2 cannot begin until the controller
has one unique receipt for every member of the frozen cohort.

The supervisor proof records both `prior_state` and the final quiescent state
for every exact identity. The prior state is not reconstructed from a service
file or from a later supervisor sample: phase 1 has already stopped the old
generation by then. The receipt validator requires the complete identity set,
rejects duplicates and unknown states, and permits phase 2 only when every
final state is inactive or absent. On launchd, the node installer consumes that
generation-bound prestate as the expected prior state for per-service
compensation; it rejects a gateway or worker found in the system domain and
rejects multiple simultaneously active gateway owners because neither topology
can be restored unambiguously.

On launchd nodes, quiescence is a bounded state transition rather than a
one-shot sample: every managed worker or gateway job must become explicitly
absent in both relevant domains, unexpected inspection errors fail closed, and
all subprocess and total deadlines use a monotonic clock. The managed worker
plist uses a 30-second `ExitTimeOut`, leaving a strict cleanup margin after
launchd's terminal kill boundary.

The cut-over's worker, control-plane, chat-gateway, and hub-tunnel supervisor
lifecycle operations are exact, non-interactive, and bounded on all three
supported managers. Manager commands run in their own process groups
under per-command bounds; multi-probe transitions and rollback share a total
monotonic deadline as well. Service definitions are staged, atomically replaced
with their parent directory fsynced, and coupled to an exact manager scope; a
failing system-scope command is never retried against a different user-scope
manager. systemd and supervisord definitions participate in the node-generation
rollback described below rather than being treated as independent best-effort
writes.

All repo-resident launchd installers additionally use
`deploy/lib/launchd-lifecycle.sh` for a per-service transaction: snapshot every
artifact, stage and validate the new plist, prove the old label absent,
atomically replace the plist, bootstrap only from proved absence, prove the new
label loaded, and only then run service health checks. EXIT or signal failure
stops the partial replacement, restores tracked artifacts in reverse order,
and restarts only the jobs recorded active in the prior state. Domain
migrations and auxiliary supervisor jobs use a deferred after-restore hook so
they start only after the primary old generation and every artifact have been
restored. Transported pre-source and rollback scripts embed the same bounded
state machine because the installed shared library is not yet trustworthy at
their execution boundary. For Qdrant, label absence is followed by explicit
removal and absence proof of the daemon-owned named container before the new
wrapper or plist is installed.

Supervisor absence alone is not a daemon-resource proof. Before source is
replaced, the node installer resolves the prior OpenClaw sandbox only from its
owner-private `managed/sandbox-name` identity (or the strictly parsed legacy
`runtime.env` field), inventories OpenShell through its node-local JSON API,
runs the old checkpoint wrapper first, and independently proves exact sandbox
absence after bounded deletion. Raw environment or command output is never
replayed into deployment evidence.

The same boundary inventories every provably node-local Docker context and
Podman connection, recording the exact daemon endpoint identities used for the
proof. Unix sockets qualify directly; unproven loopback TCP or SSH endpoints
fail closed because they may be tunnels to a remote daemon rather than proof
of local ownership. A legacy Nemo compose container is classified from its
exact service label plus an independent project, compose-path, or legacy
image-command-name corroborator; ambiguous ownership fails closed. A running
compose-only Nemo gateway is not an automatically recoverable supervisor
topology: deleting it loses the only runnable object, while restoring a unit,
plist, or supervisord program cannot recreate it. The required phase-1 rule is
to reject a running legacy container before mutation. Until the daemon gate
enforces that distinction, any node on which a legacy Nemo container exists is
ineligible for synchronized cut-over. It must first be migrated to a
manager-backed, version-pinned Nemo service with a durable sandbox identity,
checkpoint, and restore manifest, or retired by a separate operation with an
explicit container restore journal. The current reference Nemo installer does
not publish that sandbox/state contract, so a prior active Nemo gateway remains
ineligible for phase 2 even when its supervisor process is manager-backed; a
stable restarted PID alone is not continuity proof. Stopped legacy containers
are not part of the pre-source deletion set and may be retired only after the
new fleet epoch commits. Two successive empty running-container inventories
are required. An owner-only
`mac.daemon_resource_quiescence.v1` receipt is first atomically published for
the `pre_source` boundary, bound to the deployment generation, revision, and
runtime endpoint set. That same receipt must match the current endpoint set.
Every implementation proves `pre_install` and `post_install`; OpenClaw also
proves `pre_verify` and `pre_finalize` around its ownership handoff. This
prevents a restart policy from making an old container impersonate new gateway
health between lifecycle steps. A failed late proof makes the deployment fail
closed; OpenClaw rollback must itself prove exclusive shutdown or report a
second explicit failure, and its advertisement and pending-verification
artifacts are withdrawn.

The same closed-world rule applies to the optional GPU image, audio, and video
servers. Their three systemd units are not yet members of the generation
rollback manifest, so phase 1 requires all three units to be absent and a
synchronized install does not create them. A GPU node with any of those units
present is ineligible for this cut-over; media generation can be re-enabled
only after those units, wrappers, enablement intent, and health evidence join
the same prepare/rollback contract. Optional does not mean exempt from the
transaction boundary.

After supervisor installation, a separate owner-only
`mac.gateway_readiness.v1` receipt proves two stable observations of the exact
selected gateway process, with all competing gateway identities non-running.
For `gateway_impl=none`, it proves all gateway identities non-running. The post
manifest binds the phase-1, daemon-resource, and gateway-readiness receipt
digests; the latest manifest must be byte-for-byte equivalent in those
summaries.

An existing node arms automatic rollback only after complete prior source and
virtualenv backups exist. Before phase-2 writes, it also snapshots the complete
executable tree, environment and generation markers, successor gateway
definitions, and the exact prior supervisor topology. After the old OpenClaw
sandbox has checkpointed and proved absent, the node snapshots the whole
host-side OpenClaw runtime tree, including managed config, policy, workspace,
state, credentials, image identity, and publication records. That ordering is
intentional: an earlier snapshot loses the final sandbox checkpoint, while a
later snapshot captures successor configuration. A prior active OpenClaw
topology without this owner-private runtime snapshot is ineligible for phase 2.
OpenClaw's script-backed host jobs have one additional manager-owned surface:
systemd-user `${fleet}-openclaw-script-*.service`/`.timer` definitions or
launchd `com.${fleet}.openclaw-script-*.plist` definitions and their loaded
state. The runtime-tree snapshot covers their runner, job specification, and
output, but not those definitions. Phase 1 now inventories every exact-prefix
definition into an owner-private, digest-bound host-automation journal, records
systemd-user enablement or launchd loaded/disabled-override state, and quiesces
the recorded jobs before phase 2. Synchronized OpenClaw preparation accepts
host scheduling only when the exact phase-1 contract and quiescence receipt
authorize it. Rollback unloads and removes successor-only definitions, restores
the prior bytes and modes, reconstructs enablement/override intent, restarts
only jobs recorded loaded, and proves the resulting topology before publishing
its completion receipt. Loaded jobs without a safe exact definition and active
systemd oneshot services remain ineligible because they cannot be replayed
without inventing prior state.
A new node with no complete prior generation remains held and cannot have a
post manifest accepted as deployable, but requires cleanup or a retry instead
of pretending it can roll back. Any nonzero exit after phase-2 mutation first
revalidates the deployment fence while the lock renewer is still alive, then
invokes the owner-only rollback program. Loss of the fence forbids further
mutation. The original deployment failure remains the result even when
rollback succeeds.

Rollback preflights every required backup before changing supervisor state,
then uses one bounded helper to stop every exact MAC service identity and prove
the control-plane port closed. If the failed generation created an OpenClaw
sandbox, that sandbox must also checkpoint or be deleted and prove absent
before its host runtime tree is replaced. This is a cut-over eligibility gate,
not a best-effort cleanup: a generated rollback that proves only supervisor
quiescence does not yet satisfy this contract. Rollback restores source,
virtualenv, executable and environment surfaces, the prior OpenClaw tree (or
restores its prior absence), and every tracked service definition before any
service is restarted. The reviewed safe topology is the topology phase 1
actually recorded: the prior control-plane mode, exactly one of Hermes,
OpenClaw, NemoClaw, or no gateway, and the prior active/inactive/absent worker
state. The OpenClaw component's in-transaction compensation is withdraw-only;
it cannot guess Hermes because only the outer transaction owns this topology
record. Two stable process observations plus control-plane HTTP health when
applicable are required before a restore receipt is published. If restore
fails after a partial start, the helper re-quiesces every identity; if artifact
restoration fails mid-journal, reverse compensation returns artifacts to their
pre-rollback state. Either failure leaves the node held and produces no success
receipt; the failed remote transaction prevents the controller from accepting
any post manifest left by the interrupted generation.

After every node reaches `prepared`, the controller sends the complete set of
generation-bound install receipts, pending-principal heartbeats, node-authored
candidate-key challenges, and report-executor startup evidence to `prove`.
Partial proof never advances the hub. The controller journals `commit-start`
and calls `commit` with the exact epoch identity. In one database transaction,
the hub revalidates the cohort and promotes the staged principals, attestation
keys, report-executor policy, and successor holds while withdrawing service
claims. The committed receipt names the exact cohort, identity digest,
generation bundle, and successor-hold reason.

Hub commit is irreversible but not terminal. Each node is then finalized by
the exact helper digest recorded when phase 2 was armed. Finalization removes
only generation-owned barriers, locks, and retained transient artifacts and
publishes a typed receipt. A controller crash after commit is recovered by
resuming these finalizers; it never rolls nodes back. The cohort journal becomes
terminal only after all finalization receipts are durable.

Before commit, recovery first rebinds every current route to its journaled
endpoint identity. It resolves any lost hub open or prove response from the
one-use envelope, aborts the exact hub epoch, discards only unreserved pending
credentials created by that epoch, and then compensates nodes in reverse
mutation order. Current active credentials and unrelated pending credentials
are never removed. The abort result remains held.

### Attestation-key recovery and report-executor approval

Every selected loop worker receives an epoch-owned pending principal and
candidate attestation key while its current principal and key remain valid.
Secret material is relayed only through owner-private files, installed under
the node deployment fence, and proved after restart by a node-authored
challenge response. The worker never receives attestation-rotation authority,
and a bound worker credential cannot call an administrative recovery endpoint.

For an OpenShell-enabled loop worker, the controller then fetches the exact
current `report_repository_executor_attestation` plus the matching startup
self-test timestamp and installs approval with an admin-only compare-and-set.
The hub derives `report_repository_executor`; workers cannot submit that marker
or its approval themselves. A mismatch or failed approval explicitly revokes
eligibility. Both the per-node preparation gate and the atomic hub epoch commit
revalidate the derived marker and its matching startup proof. This path is the
same for launchd, systemd, supervisord, and the SSH-managed selected GKE worker
pods; a selected target that cannot complete it remains held.

## 4. Enable synchronized work only after the epoch is proven

Keep pipeline and landing disabled during the fleet cut-over. Once all agents
report the new source commit, generation, bound worker principal, idle/healthy
state, and the exact successor-hold receipt, follow
`docs/work-package-pipeline-activation.md` for the separate activation deploy.
Retain the successor holds through activation and release only the intended
canary or work cohort after the enabled pipeline has passed its runtime checks.
