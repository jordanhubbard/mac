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
