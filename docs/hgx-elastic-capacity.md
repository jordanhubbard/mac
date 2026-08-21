# HGX elastic capacity

MAC has an operator-side controller for adding fungible HGX capacity without
weakening the machine-onboarding trust boundary:

```console
$ mac admin hgx capacity status --pending-requests 2 --max-sessions 6
$ mac admin hgx capacity plan --pending-requests 2 --headroom 1 --max-sessions 6
$ mac admin hgx capacity execute --pending-requests 2 --headroom 1 --max-sessions 6
$ mac admin hgx capacity mark-onboarded <session-id> --agent-id <agent-id>
```

`status` and `plan` are read-only. They run `hgx list` through
`mac.hgx_provider.HgxProvider`, inspect the durable controller receipt, and do
not create, stop, resume, or delete provider sessions. The explicit `execute`
command creates capacity. On an HGX-enabled hub, the optional background
autoscaler invokes the same bounded controller from durable provisioning
demand; provider work never runs on a dispatcher or HTTP thread.

Enable it only on the authenticated hub that owns HGX provider credentials:

```text
MAC_HGX_AUTOSCALE_ENABLED=1
MAC_HGX_AUTOSCALE_MAX_SESSIONS=10
MAC_HGX_AUTOSCALE_SCALE_UP_STABILIZATION_SECONDS=120
MAC_HGX_AUTOSCALE_SCALE_UP_STEP=1
MAC_HGX_AUTOSCALE_COOLDOWN_SECONDS=300
MAC_HGX_AUTOSCALE_SCALE_DOWN_STABILIZATION_SECONDS=3600
MAC_HGX_AUTOSCALE_SCALE_DOWN_STEP=1
MAC_HGX_AUTOSCALE_SPARE_MIN_AGE_SECONDS=3600
```

The hub process must be able to execute the configured `MAC_HGX_BINARY`
non-interactively with a valid owner credential. Enabling the service without
that credential is observable as a provider error and never falls back to
unverified capacity.

## Readiness contract

Every created session uses the current HGX CLI contract for an explicit
`standard-dind` instance. It also passes explicit placement and resource
arguments instead of inheriting provider defaults:

```text
--cluster gke-newhouse --gpu 1 --memory 64Gi --cpu 8
```

The CLI exposes `--cluster`, `--gpu`, `--memory-gib`, and `--cpu`; cluster
identifiers are restricted to a safe argv shape, GPU is bounded to 0..8,
memory to 8..256 GiB, and CPU to 1..64. The controller records the immutable
provider session ID immediately after the provider accepts the create request.
That request is not readiness evidence.

## Linux network capability (`/dev/net/tun`, `NET_ADMIN`)

**An HGX session is not guaranteed to be able to run kernel-mode Tailscale, a
VPN client, or anything else that opens a TUN device.** On `gke-newhouse`, a
`standard-dind` pod has no `/dev/net/tun` and is not granted `CAP_NET_ADMIN`:
`tailscaled` fails with `CreateTUN("tailscale0") failed; /dev/net/tun does not
exist`, and its `iptables` calls are denied with "Permission denied (you must be
root)" even when the process really is root. Those two symptoms are one cause —
the pod spec withholds the capability — so a missing kernel module is not the
thing to go fix.

Capability is a property of the cluster and profile you provision into. `hgx
create` exposes no flag that requests it, so neither this controller nor
`deploy/` can ask for it. Check a session rather than assuming:

```console
$ hgx ssh <session-or-name> -- 'ls /dev/net/tun 2>/dev/null || echo NO-TUN'
$ hgx ssh <session-or-name> -- 'grep CapEff /proc/self/status'
```

| Cluster / profile | `/dev/net/tun` | `NET_ADMIN` | Kernel-mode Tailscale |
|---|---|---|---|
| `gke-newhouse`, `standard` pod (2026-08-20) | absent | not granted | no — userspace mode only |
| `gke-newhouse`, `standard-dind` (what this controller creates) | expected absent | expected absent | probe before relying on it |
| any other cluster/profile | unverified | unverified | probe before relying on it |

Only the first row is a direct observation: one `hgx`-provisioned `standard`
pod, whose `tailscaled` log carried both symptoms above. The rest is
deliberately left unverified. Recording a guess here would be worse than
recording nothing, because the failure it produces surfaces in a `tailscaled`
log long after provisioning succeeded. Add a row when you have run the probe.

MAC's answer to a node without the capability is not to fail: it is
`deploy/install-tailscale.sh`, which detects the missing device and starts
`tailscaled` in Tailscale's userspace networking mode. Such a node joins the
mesh and gets a Tailscale IP, but the host does not route into the mesh —
outbound mesh traffic must go through the local SOCKS5/HTTP proxy and inbound
reachability is limited to what `tailscale serve` publishes. That is workable
for a worker and poor for a hub. See `QUICKDEMO.md` for the operator-facing
version.

If a provider build does expose a capability-granting argument, pass it through
instead of forking the create contract:

```console
$ mac admin hgx capacity execute --pending-requests 1 \
    --create-arg=--some-cap-flag --create-arg=NET_ADMIN
```

Use the `--create-arg=VALUE` form. A pass-through argument almost always starts
with a dash, and `--create-arg --some-cap-flag` makes `argparse` read the flag
as a missing value rather than as the argument you meant.

`--create-arg` is repeatable and bounded: at most 8 arguments, each 1..64
characters drawn from letters, digits and `-_=:./,+`, and
`--cluster`/`--gpu`/`--memory`/`--cpu`/`--name` are rejected because the policy
and the immutable session name own them. The accepted arguments are appended
after the policy-derived ones and echoed in the `status`, `plan`, and `execute`
documents under `policy.create_extra_args`, so what a run actually requested is
visible without re-deriving it. MAC does not guess a flag name: an empty
`--create-arg` list is the correct default until the provider documents one.

The background autoscaler takes the same list, whitespace-separated:

```text
MAC_HGX_AUTOSCALE_CREATE_ARGS=--cap-add=NET_ADMIN --device=/dev/net/tun
```

It is validated through the same policy at configuration time, so a rejected
value surfaces as `hgx.autoscaler.configuration_invalid` and leaves the
autoscaler inactive rather than failing once per reconciliation.

Before a session is reported as attested capacity, the controller:

1. addresses it only by immutable session ID;
2. waits for `hgx status <session-id>` to stop failing; and
3. requires `hgx ssh <session-id> -- ...` to return the unpredictable nonce
   generated by `HgxProvider.attest_ssh`.

An SSH failure is recorded by failure class without raw provider output. A
failed session never counts toward `min-ready`.

## Bounds and cooldown

The desired ready count is:

```text
min(max-sessions, max(min-ready, pending-requests + headroom))
```

`max-sessions` bounds all live provider inventory, including unrelated live
HGX sessions, so the controller does not evade a provider-wide safety limit.
However, unrelated sessions are not assumed idle: only controller-created,
nonce-attested, not-yet-onboarded sessions satisfy pending provisioning
demand. Five existing busy workers plus one pending request therefore plans
one new session when `max-sessions` is greater than five. Existing untracked
workers are not re-attested by the capacity controller.

One execution creates at most `max-create-per-run` sessions (default one).
`cooldown-seconds` prevents a later invocation from immediately starting
another step. Provider HTTP 429/quota errors are reduced to the
secret-free `provider_quota_exhausted` failure class rather than being
misreported as satisfied demand.

## Autoscaling curve

The automatic curve is intentionally asymmetric:

1. a task-bound `dispatch.no_eligible_agent` request is durable immediately,
   but it must remain actionable for `scale-up-stabilization-seconds` before it
   contributes to desired capacity;
2. each reconciliation creates at most `scale-up-step` sessions;
3. the controller cooldown spaces later scale-up steps;
4. demand returning to zero starts a separate, longer
   `scale-down-stabilization-seconds` window; and
5. one scale-down pass retires at most `scale-down-step` old surplus sessions.

Task-bound requests are reconciled before capacity math. Requests for terminal
tasks or tasks already assigned are cancelled instead of becoming phantom
demand. Legacy dispatch/review rows without a task ID are also cancelled
because their liveness cannot be proved. Reviewer shortages and service-role
requests remain available to their matching provisioners but do not create a
generic HGX coding worker. The raw, sustained, ignored-by-reason, and
zero-demand-age counts are emitted as `hgx.autoscaler.*` observability metrics.

The receipt defaults to `~/.mac/hgx-elastic-capacity.json`. Provider mutation
commands create or update it using an fsynced, mode-0600 atomic replacement. It
contains immutable session IDs, timestamps, secret-free outcome classes, and
the next required action. A non-blocking process lock rejects overlapping
`execute` invocations before either can create capacity.

## Provisioning requests and onboarding

The durable signal store remains `ProvisioningService.list_pending_requests()`
(or `GET /provisioning/requests?status=pending`), but the automatic HGX demand
count is the subset of task-bound `dispatch.no_eligible_agent` rows that still
refer to an open, unowned task. Do not pass the total store count to the manual
controller: it also contains reviewer and specialized service-role requests
that a generic coding worker cannot satisfy.

The autoscaler registers a wake-only request listener and then polls the
durable rows on its own background thread. The older synchronous
`register_provisioner` hook remains an auto-fulfillment extension point, but it
must return `None` until onboarding has registered a real agent. An HGX session
ID is not a MAC agent ID and must never be used to fulfill a provisioning
request.

After nonce SSH attestation, the controller stops and persists a
`prepare_fungible_onboarding` next action. The operator or a separately
reviewed automation path must supply:

- the fleet agent name;
- the hub agent;
- a reviewed draining/degraded fungible placeholder; and
- endpoint-bound worker credentials.

Those inputs feed `deploy/deploy-mac-fleet.sh --prepare-fungible-onboarding`. The capacity
controller does not invent credentials or silently mark provisioning requests
fulfilled.

Once onboarding has registered the real agent and the corresponding
provisioning request is fulfilled, consume the capacity receipt explicitly:

```console
$ mac admin hgx capacity mark-onboarded <immutable-session-id> \
    --agent-id <registered-mac-agent-id>
```

This is an idempotent receipt-only mutation; it performs no provider action.
The onboarded session continues to consume the provider-wide
`max-sessions`/quota bound, but it no longer counts as available supply for a
future pending request.

## Retirement

Automatic retirement is restricted to controller-created sessions that never
became registered MAC agents. They must be surplus after the scale-down
stabilization window and older than `spare-min-age-seconds`; deletion proceeds
one bounded step at a time using the immutable session ID.

An onboarded session is never automatically deleted by this controller.
Retiring a registered worker also requires draining its leases, revoking its
worker credential, tombstoning the agent, and updating the authoritative
`fleets.yaml` record. Until that multi-resource lifecycle transaction exists,
the autoscaler fails closed at this boundary instead of leaving MAC believing a
deleted provider instance is still a live worker.
