# Fleet registry schema

`~/.mac/fleets.yaml` is the authoritative operator registry for fleet SSH
routes and deployment topology. Set `MAC_DEPLOY_FLEETS_CONFIG` or pass
`--fleets-config` to use another file. Do not keep a second deployment-only
copy of the topology.

## Multi-fleet form

The normal form uses a `fleets` mapping. Fleet entries are selected by their
`hub_agent`; the outer key is a stable operator label and may match the hub
name.

```yaml
version: 1
fleets:
  default:
    sample: false
    fleet_name: default
    hub_agent: hub
    control_port: 8789
    agents:
      hub:
        target: operator@hub.example.net
        os: linux
      worker-1:
        target: operator@worker-1.example.net
        os: linux
```

`fleets` may alternatively be a list of mappings that each declare
`hub_agent`.

## Single-fleet flat form

For one fleet, the wrapper may be omitted. This is the same read-compatible
form accepted by `scripts/setup-fleet.py`:

```yaml
sample: false
fleet_name: default
hub_agent: hub
control_port: 8789
agents:
  hub:
    target: operator@hub.example.net
    os: linux
```

In either fleet form, `agents` may be a mapping keyed by agent name or a list
whose entries contain `name`. When a mapping entry also contains `name`, it
must agree with the mapping key.

## Static and fungible instances

`instance_kind` is a first-class agent lifecycle property. It defaults to
`static` for named, durable machines such as conversational fleet members.
Set it to `fungible` only for a replaceable instance created dynamically by a
provider API or CLI such as `hgx create`.

```yaml
agents:
  worker-4:
    instance_kind: fungible
    target: operator@current-provider-route
    os: linux
    supervisor: supervisord
```

The explicit `--prepare-fungible-onboarding` operation requires this value and
binds the live SSH host key plus machine identity before mutation. It will not
convert an existing static hub record. The phase-zero placeholder is registered
atomically as `instance_kind=fungible`, `status=draining`, and
`health_status=degraded`; it remains nondispatchable until the subsequent
normal typed deployment proves and commits the worker generation.

## Route-only versus deployable entries

A compact registry used only for login or SSH routing may omit deployment
metadata. Deployment deliberately fails closed for those entries. A fleet is
deployable only when setup has written `sample: false`; this prevents the
deployer from merging a route with the checked-in sample topology and applying
the wrong identity or services to a host.

Targets use `user@host` or `user@host:port`. Keep credentials out of this
file: use identity-file references and environment/secret references rather
than private-key bytes or tokens.

## Worker confinement

`worker.openshell_required` controls whether fleet deploy must bootstrap and
fail-close the OpenShell task runtime on that node. It is optional. A pure
worker with `hermes.gateway_impl: none` defaults to `true`; conversational
nodes default to `false` unless the field is explicitly enabled.

```yaml
agents:
  worker-1:
    target: operator@worker-1.example.net
    os: linux
    supervisor: supervisord
    hermes:
      gateway_impl: none
    worker:
      mode: loop
      openshell_required: true
```

Required nodes automatically run the idempotent OpenShell bootstrap with
enforcement and fail-closed execution during every deploy. This is deliberate
for ephemeral pods: `~/.mac` may survive while `~/.local/bin/openshell` does
not. The deploy must rebuild missing runtime prerequisites rather than accept a
heartbeat from a worker that cannot execute a sandboxed coding route.

## Worker repository credentials

`worker.github_credentials_required` controls whether fleet deploy must verify
an authenticated GitHub HTTPS credential before it drains the node or replaces
source. A pure worker with `hermes.gateway_impl: none` defaults to `true` so a
fresh pod cannot register as a code executor that can edit locally but cannot
clone or publish. Conversational nodes default to `false`. Set it to `false`
only for an intentionally public/read-only executor.

The operator-side deploy resolves the credential in this order:
`MAC_DEPLOY_GH_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, then the existing
`gh auth token --hostname github.com` keychain login. It reports only the
source name and streams the value over the resolved SSH route on stdin. The
credential is stored in the owner-only managed runtime as `GH_TOKEN` and is
forwarded into OpenShell through its private mode-`0600` environment bundle;
it is never placed in the SSH command, fleet registry, task metadata, or logs.

```yaml
agents:
  worker-1:
    hermes:
      gateway_impl: none
    worker:
      mode: loop
      github_credentials_required: true
```
