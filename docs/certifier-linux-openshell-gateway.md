# Linux OpenShell gateway for a Darwin certifier controller

The work-package certifier requires OpenShell with
`landlock.compatibility: hard_requirement`. A macOS host cannot provide that at
all: as of [ADR 0015](adr/0015-macos-nodes-are-host-installs.md) a darwin node
is a plain host install with no container runtime (isolation posture
`macos_host`), and the Docker Desktop path that preceded it did not expose the
Linux host Landlock boundary either. The certifier must not weaken its policy to
run locally. Keep the MAC control plane on Darwin and route every certification
sandbox operation through a Linux OpenShell gateway.

## Topology and trust boundary

```text
MAC hub certifier process
  -> http://127.0.0.1:17671
  -> launchd-managed, host-key-verified SSH local forward
  -> Linux 127.0.0.1:17670
  -> OpenShell Docker driver and hard-Landlock sandbox
```

The plaintext OpenShell endpoint exists only on loopback at both ends. SSH
provides transport authentication and encryption. The Linux bootstrap installs
an iptables/ip6tables chain that permits gateway traffic from loopback and
Docker bridge interfaces and drops it from every other interface, including
mesh interfaces. Candidate sandboxes never receive the endpoint, SSH material,
hub credentials, or landing credentials.

## Installation

1. Resolve both current targets from `~/.mac/fleets.yaml`. Do not infer an SSH
   target from an agent name, prior deployment, or `known_hosts`.
2. Deploy the selected Linux certifier-gateway node with OpenShell enabled and
   fail closed:

   ```console
   MAC_DEPLOY_OPENSHELL=1 \
   MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE='ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:<reviewed-digest>' \
   MAC_DEPLOY_OPENSHELL_ARGS='--enable --fail-closed' \
     deploy/deploy-mac-fleet.sh --hub <hub-agent> <linux-gateway-agent>
   ```

3. On the Darwin hub, install the persistent tunnel using the resolved Linux
   `user@host` target:

   ```console
   ~/.mac/src/mac/deploy/openshell/install-certifier-gateway-tunnel.sh \
     --target <linux-user@resolved-host> \
     --openshell-bin ~/.mac/bin/openshell
   ```

   Installation is non-interactive and fail closed. It requires an existing
   host-key entry and working key-based SSH authentication; it never accepts a
   password prompt or a new host key.

4. Persist the certifier-only endpoint on the hub during deployment:

   ```console
   MAC_DEPLOY_CERTIFIER_OPENSHELL_GATEWAY_ENDPOINT=http://127.0.0.1:17671 \
     deploy/deploy-mac-fleet.sh --hub <hub-agent> <hub-agent>
   ```

   `mac.deploy_env` writes this as
   `MAC_CERTIFIER_OPENSHELL_GATEWAY_ENDPOINT` on the hub and removes it from
   spokes. The production certifier accepts only an exact loopback HTTP URL and
   translates it to the OpenShell CLI's `OPENSHELL_GATEWAY_ENDPOINT`; arbitrary
   remote plaintext endpoints are rejected.

## Mandatory cut-over proof

First keep both work-package switches disabled, pause the project, and hold all
workers. In that fail-closed state:

- `openshell status` must succeed through the loopback endpoint from the exact
  service HOME and CLI used by the hub.
- A sandbox using the checked-in certification policy must create and delete
  successfully on the Linux gateway.
- The certification image must be referenced by an immutable registry digest.

The canary helper requires the managed pipeline to be alive, so enable pipeline
and landing together only after those gateway proofs pass, while the project is
still paused and every worker remains held:

```console
MAC_DEPLOY_WORK_PACKAGE_PIPELINE_ENABLED=1 \
MAC_DEPLOY_WORK_PACKAGE_LANDING_ENABLED=1 \
  deploy/deploy-mac-fleet.sh --hub <hub-agent> <hub-agent>
```

Keep ordinary task ingress frozen for both canaries. Admit one canary at a time,
open only the selected mutation worker's claim window, and restore its agent
hold plus the project pause immediately after the mutation task is claimed. The
already-claimed mutation may finish, while no unrelated work can cross the
one-way certification/landing boundary.

- The negative canary must pass candidate-owned tests but fail the image-owned
  frozen contract, with no landing receipt and no movement of the canonical
  ref.
- The positive canary must certify, land exactly once, finalize, and leave the
  remote canonical ref at the receipt SHA.

If either canary fails, keep every worker held and the project paused. Raise the
Andon on any still-active canary package, then redeploy the hub with both
switches explicitly disabled:

```console
mac work-package pause <package-id> --plan-version 1 --epoch 1 \
  --reason "cut-over canary failed" --actor cutover-canary
MAC_DEPLOY_WORK_PACKAGE_PIPELINE_ENABLED=0 \
MAC_DEPLOY_WORK_PACKAGE_LANDING_ENABLED=0 \
  deploy/deploy-mac-fleet.sh --hub <hub-agent> <hub-agent>
```

Do not remove the tunnel, discard the canary evidence, or unfreeze ordinary
task ingress during failure handling. Only after the negative and positive
receipts both pass may the project be activated and the worker holds removed.

## Operations and removal

The launchd label is `com.mac.certifier-openshell-tunnel`. Logs are written to
`~/.mac/logs/certifier-openshell-tunnel.{out,err}.log`. Re-running the installer
atomically replaces the property list and proves gateway health before
returning success.

Remove the tunnel only after disabling both pipeline and landing:

```console
~/.mac/src/mac/deploy/openshell/install-certifier-gateway-tunnel.sh --remove
```
