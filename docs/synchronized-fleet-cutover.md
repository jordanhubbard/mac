# Synchronized Fleet Cut-over

The fleet deployer uses a three-phase epoch: hold and drain the complete
selected cohort, deploy and prove every new generation, then release the exact
cohort in one hub transaction. A worker that cannot join the epoch remains
held; the deployer never degrades an exact cut-over into partial release.

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
hub OpenAPI document contains all three routes:

- `/agents/{agent_id}/dispatch-hold/acquire`
- `/agents/{agent_id}/dispatch-hold/release`
- `/agents/dispatch-hold/release-batch`

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
  <hub-agent> <agent-2> <agent-3> <agent-4> <agent-5>
```

Before phase 1, the deployer reads every selected hub row and validates the
whole authority. A mismatch anywhere causes zero hold CAS, drain, or worker
stop. During phase 1, each authorized hold is replaced only by an
`expected_dispatch_hold=true` and exact-previous-reason CAS. A post-preflight
operator hold or stale controller state fails before drain.

Phase 3 accepts only readiness records whose stable IDs exactly equal the
frozen selected set and, in exact mode, whose holds are all owned by this
deployment. The hub batch response must carry the same epoch and IDs. The
deployer then re-reads every selected row and requires every hold to be clear.
The durable receipt must show:

```text
cohort_size == deployment_holds_released
operator_holds_preserved == 0
agent_ids == exact selected stable ids
```

If a response is lost, retrying the same epoch is safe because the hub records
an idempotent lifecycle receipt. If an earlier agent was adopted before a later
race aborts phase 1, do not clear it manually: it remains safely held under the
deployment reason. Inspect the hub lifecycle receipt and the node's owner-only
`~/.mac/deploy-dispatch-hold.json`, then use the documented stale-lock takeover
only after proving the previous controller is gone.

## 4. Enable synchronized work only after the epoch is proven

Keep pipeline and landing disabled during the fleet cut-over. Once all agents
report the new source commit, generation, bound worker principal, idle/healthy
state, and an exact zero-preserved-hold receipt, follow
`docs/work-package-pipeline-activation.md` for the separate activation deploy.
