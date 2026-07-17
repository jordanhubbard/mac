# Linux OpenShell gateway for a Darwin certifier controller

The work-package certifier requires OpenShell with
`landlock.compatibility: hard_requirement`. Docker Desktop on a macOS hub does
not expose the Linux host Landlock boundary, so the certifier must not weaken
that policy to run locally. Keep the MAC control plane on Darwin and route only
certification sandbox operations through a Linux OpenShell gateway.

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

   ```bash
   MAC_DEPLOY_OPENSHELL=1 \
   MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE='ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:<reviewed-digest>' \
   MAC_DEPLOY_OPENSHELL_ARGS='--enable --fail-closed' \
     deploy/deploy-mac-fleet.sh --hub <hub-agent> <linux-gateway-agent>
   ```

3. On the Darwin hub, install the persistent tunnel using the resolved Linux
   `user@host` target:

   ```bash
   ~/.mac/src/mac/deploy/openshell/install-certifier-gateway-tunnel.sh \
     --target <linux-user@resolved-host> \
     --openshell-bin ~/.mac/bin/openshell
   ```

   Installation is non-interactive and fail closed. It requires an existing
   host-key entry and working key-based SSH authentication; it never accepts a
   password prompt or a new host key.

4. Persist the certifier-only endpoint on the hub during deployment:

   ```bash
   MAC_DEPLOY_CERTIFIER_OPENSHELL_GATEWAY_ENDPOINT=http://127.0.0.1:17671 \
     deploy/deploy-mac-fleet.sh --hub <hub-agent> <hub-agent>
   ```

   `mac.deploy_env` writes this as
   `MAC_CERTIFIER_OPENSHELL_GATEWAY_ENDPOINT` on the hub and removes it from
   spokes. The production certifier accepts only an exact loopback HTTP URL and
   translates it to the OpenShell CLI's `OPENSHELL_GATEWAY_ENDPOINT`; arbitrary
   remote plaintext endpoints are rejected.

## Mandatory pre-activation proof

Before enabling either work-package pipeline or landing:

- `openshell status` must succeed through the loopback endpoint from the exact
  service HOME and CLI used by the hub.
- A sandbox using the checked-in certification policy must create and delete
  successfully on the Linux gateway.
- The certification image must be referenced by an immutable registry digest.
- The negative canary must pass candidate-owned tests but fail the image-owned
  frozen contract, with no landing receipt and no movement of the canonical
  ref.
- The positive canary must certify, land exactly once, finalize, and leave the
  remote canonical ref at the receipt SHA.
- During both canaries, ordinary task ingress must remain frozen so no
  unreviewed workload crosses the one-way certification/landing boundary.

Only after all proofs pass may the hub be redeployed with
`MAC_DEPLOY_WORK_PACKAGE_PIPELINE_ENABLED=1` and
`MAC_DEPLOY_WORK_PACKAGE_LANDING_ENABLED=1`.

## Operations and removal

The launchd label is `com.mac.certifier-openshell-tunnel`. Logs are written to
`~/.mac/logs/certifier-openshell-tunnel.{out,err}.log`. Re-running the installer
atomically replaces the property list and proves gateway health before
returning success.

Remove the tunnel only after disabling both pipeline and landing:

```bash
~/.mac/src/mac/deploy/openshell/install-certifier-gateway-tunnel.sh --remove
```
