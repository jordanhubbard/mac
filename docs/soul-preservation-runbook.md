# Soul Preservation Runbook

`mac` records persona identity as durable URI references, not as durable
content. The "soul" — `SOUL.md`, `USER.md`, `MEMORY.md`, conversational
behavior — lives on the Hermes side. `mac` stores `persona.soul_ref` and
`persona.memory_scope` as pointers so that, when a Hermes process restarts or
migrates hardware, the identity can be reattached without loss.

This runbook documents the recovery contract and demonstrates an operational
proof. The corresponding smoke test is
`tests/test_soul_preservation.py::test_soul_survives_hermes_process_loss`.

## What `mac` guarantees

For every `(tenant, persona, hermes_instance, platform_binding)` quadruple
registered against `mac`:

1. `persona.soul_ref` and `persona.memory_scope` are durably persisted in the
   `personas` table and never overwritten by re-registration with `None`
   metadata (see `_resolved_json_column`).
2. `hermes_instance.persona_id` survives across re-registrations of the
   instance — the natural key `(tenant_id, name)` is what we match against.
3. `platform_bindings` for the instance remain attached as long as the
   `(tenant_id, platform, external_id)` triple stays stable.
4. `GET /hermes-instances/{id}/context` returns a single recovery payload
   containing tenant, instance, persona, all bindings, and the memory contract.
5. Every identity write produces an `events` row, so the audit stream shows
   when an instance was last registered, when its bindings changed, and which
   actor performed the change.

What `mac` does *not* do:

- Store soul content. `SOUL.md` and `MEMORY.md` are URIs.
- Re-create the Hermes process. The runbook below assumes Hermes is restarted
  by an external supervisor (systemd, k8s, a human).
- Replay conversation history. Conversation continuity is handled by Hermes;
  `mac` only records that a task originated from a binding.

## Recovery procedure

Assume Hermes has lost its process state (host died, container restarted,
operator killed the daemon). `mac` is still up.

1. **Fetch the recovery payload.**

   ```console
   curl $MAC_URL/hermes-instances/<instance_id>/context
   ```

   Response shape:
   ```json
   {
     "tenant": {...},
     "hermes_instance": {"name": "assistant-one", "persona_id": "persona_...", ...},
     "persona": {"soul_ref": "hermes://...", "memory_scope": "hermes://...", ...},
     "platform_bindings": [...],
     "memory_contract": {
       "personality_authority": "hermes",
       "user_memory_authority": "hermes",
       "operational_provenance_authority": "mac",
       "soul_ref": "hermes://...",
       "memory_scope": "hermes://..."
     }
   }
   ```

2. **Materialize the soul.** Hermes startup logic reads `persona.soul_ref` and
   `persona.memory_scope` from the payload and re-hydrates from whatever URI
   scheme it uses (filesystem, object store, vector index). This is
   Hermes-side; the URI scheme is opaque to `mac`.

3. **Re-register with the same instance id.** Idempotent registration ensures
   `last_seen_at` advances and `metadata` augments without wiping. Use the
   adapter:

   ```python
   adapter.register_identity(
       tenant_name=ctx["tenant"]["name"],
       persona_name=ctx["persona"]["name"],
       instance_name=ctx["hermes_instance"]["name"],
       soul_ref=ctx["persona"]["soul_ref"],
       memory_scope=ctx["persona"]["memory_scope"],
       platform_bindings=[
           PlatformBindingSpec(b["platform"], b["external_id"], b["display_name"])
           for b in ctx["platform_bindings"]
       ],
   )
   ```

4. **Verify identity continuity.** The returned `hermes_instance.id` and
   `persona.id` MUST match the pre-restart values. If they do not, the natural
   key (`tenant_id`, `name`) drifted — investigate before continuing.

5. **Resume work.** Any tasks already in flight under the pre-restart instance
   continue to belong to it; new conversations bind to the same persona via
   the same `platform_binding_id`.

## Audit trail

After recovery, `GET /events?subject_type=hermes_instance` returns the
sequence of identity events. The smoke test asserts that re-registration
produces no duplicate persona / instance / binding rows and that the
`hermes_context` payload is functionally identical pre- and post-restart.

## Failure modes

- **`hermes_instances.last_seen_at` is registration freshness, not worker
  liveness.** It advances when the Hermes identity re-registers. Use the
  `agents.last_seen_at`, lease, and heartbeat records to decide whether `hub`,
  `worker-1`, `worker-2`, or another worker process is alive and claim-capable.
- **`memory_scope` URI unreachable.** `mac` does not validate that the URI
  resolves — that's Hermes's responsibility. A broken URI manifests as a
  Hermes startup failure, not a `mac` error.
- **Tenant name collision after a long outage.** If someone reuses the tenant
  name for a different tenant id, registration upserts onto the old row.
  Convention: never recycle tenant names; tombstone instead.
