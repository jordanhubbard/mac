# Synchronized Fleet Cut-over

The fleet deployer uses a three-phase epoch: hold and drain the complete
selected cohort, deploy and prove every new generation, then atomically hand
the deployment holds to one successor hold in the hub transaction. No selected
worker becomes dispatchable between deployment and synchronized-pipeline
activation. A worker that cannot join the epoch remains held; the deployer
never degrades an exact cut-over into a partial transition.

## Architecture and acceptance invariants

The cut-over is complete only when all of these invariants hold together. A
change that does not advance or protect one of them belongs outside the
cut-over.

| Invariant | Enforcement boundary | Required evidence |
| --- | --- | --- |
| Immutable release | Source commit, certifier image, and worker runtime are exact digest-pinned identities | CI publication receipts and local anonymous-pull verification name the same commit |
| Exact cohort | Stable agent IDs and current targets come from the frozen fleet registry | Selected set and release receipt contain the same IDs exactly once |
| Quiescent boundary | Every selected agent is held, idle, healthy, task-free, and service-claim-free before commit | Per-agent arm records and the hub transaction revalidate the frozen readiness expectations |
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
hub OpenAPI document contains all seven routes required by the cohort deployer:

- `/agents/{agent_id}/dispatch-hold/acquire`
- `/agents/{agent_id}/dispatch-hold/release`
- `/agents/dispatch-hold/release-batch`
- `/agents/dispatch-hold/transition-batch`
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

Before phase 1, the deployer reads every selected hub row and validates the
whole authority. A mismatch anywhere causes zero hold CAS, drain, or worker
stop. During phase 1, each authorized hold is replaced only by an
`expected_dispatch_hold=true` and exact-previous-reason CAS. A post-preflight
operator hold or stale controller state fails before drain.

Phase 3 accepts only readiness records whose stable IDs exactly equal the
frozen selected set and whose holds are all owned by this deployment. The hub
batch response must carry the same epoch and IDs. The transaction replaces
every deployment hold directly with the exact successor reason, withdraws any
service claims, and never commits an unheld row. The deployer then re-reads
every selected row and requires every agent to remain held by that successor.
The durable receipt must show:

```text
schema == mac.fleet_release_receipt.v2
outcome == successor_hold
cohort_size == deployment_holds_released
cohort_size == successor_holds_installed
operator_holds_preserved == 0
agent_ids == exact selected stable ids
successor_hold_reason == exact requested reason
```

Omitting `--successor-hold-reason` retains the legacy atomic-release behavior
and its `mac.fleet_release_receipt.v1` receipt. Use that mode only when the
pipeline is already active and immediate dispatch is intended; it is not the
pre-activation synchronized cut-over path.

If a response is lost, retrying the same epoch is safe because the hub records
an idempotent lifecycle receipt. If an earlier agent was adopted before a later
race aborts phase 1, do not clear it manually: it remains safely held under the
deployment reason. Inspect the hub lifecycle receipt and the node's owner-only
`~/.mac/deploy-dispatch-hold.json`, then use the documented stale-lock takeover
only after proving the previous controller is gone.

### Attestation-key recovery and report-executor approval

Every selected loop worker is reconciled while its node-local deployment lock
and hub dispatch hold are still active. The worker first emits a secret-free
signed nonce probe. A valid probe leaves the key untouched. A missing or stale
probe may be recovered only through the admin-only hub endpoint; the new key is
relayed hub -> deployment controller -> worker in owner-only one-use files,
atomically installed in `~/.mac/mac.env`, and then proved again after a service
restart. The worker no longer receives `--rotate-missing-attestation-key` or
`--rotate-invalid-attestation-key`, and a bound worker credential cannot call a
rotation endpoint.

For an OpenShell-enabled loop worker, the controller then fetches the exact
current `report_repository_executor_attestation` plus the matching startup
self-test timestamp and installs approval with an admin-only compare-and-set.
The hub derives `report_repository_executor`; workers cannot submit that marker
or its approval themselves. A mismatch or failed approval explicitly revokes
eligibility. Both the per-node arm gate and the atomic fleet
release-or-transition transaction revalidate the derived marker and its
matching startup proof. This path is the
same for launchd, systemd, supervisord, and the SSH-managed selected GKE worker
pods; a selected target that cannot complete it remains held.

## 4. Enable synchronized work only after the epoch is proven

Keep pipeline and landing disabled during the fleet cut-over. Once all agents
report the new source commit, generation, bound worker principal, idle/healthy
state, and the exact successor-hold receipt, follow
`docs/work-package-pipeline-activation.md` for the separate activation deploy.
Retain the successor holds through activation and release only the intended
canary or work cohort after the enabled pipeline has passed its runtime checks.
