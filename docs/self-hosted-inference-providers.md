# Self-hosted inference providers

MAC can converge a GPU-backed OpenAI-compatible inference server from typed,
host-local desired state.  The resource file is
`$MAC_HOME/inference-providers.json` (normally
`~/.mac/inference-providers.json`); host addresses, capacity, and model choices
therefore stay out of the repository.  Credentials are not supported in this
resource or written to the generated router registration.

The file has this shape:

```json
{
  "schema": "mac.self_hosted_inference_providers.v1",
  "providers": [
    {
      "provider_id": "site-model",
      "image": "REGISTRY/IMAGE@sha256:DIGEST",
      "model": "ORGANIZATION/MODEL",
      "model_revision": "IMMUTABLE_HEXADECIMAL_COMMIT_REVISION",
      "router_base_url": "http://HOST:PORT/v1",
      "bind_host": "127.0.0.1",
      "port": 8000,
      "priority": 0,
      "gpu_count": 1,
      "min_free_memory_mib": 16000
    }
  ]
}
```

Both image digest and hexadecimal model commit revision are mandatory.  Each provider gets a
persistent cache under `$MAC_HOME/inference-providers/<provider-id>/model-cache`.
The router registration names the exact model; it never installs a wildcard
route.

## Lifecycle

Commands are dry-run by default:

```console
mac-inference-provider reconcile
mac-inference-provider reconcile --apply
mac-inference-provider health
mac-inference-provider reconcile --allow-upgrade --apply
mac-inference-provider remove --apply
```

Reconciliation checks GPU count and free memory before pulling or starting a
container.  An exact existing container is left alone.  A stopped exact
container is started.  Identity drift reports `upgrade_required`; replacement
only happens with the explicit upgrade flag.  The replacement image is pulled
before the old container is stopped, and failed replacement health rolls the
old container back.

Health loss is never an instruction to stop, replace, or remove a container.
It reports `degraded` and withholds router registration.  Startup probes are
bounded and provider-jittered rather than driven by a synchronized background
loop.  Removal is explicit, unregisters only the named providers, and preserves
the model cache so an outage or accidental removal cannot trigger a redownload
storm.

Successful convergence atomically merges exact provider registrations into
`$MAC_HOME/mac.env` while retaining unrelated providers and settings.  Restart
or reload the independently supervised router after changing that file; this
tool does not couple provider health to router or hub process restarts.
