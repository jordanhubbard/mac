!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Field note: HGX session `c902fab4d55f` — realize capacity via preserve-nothing + REPLACE

**Task**: Using session A's inspection verdict for HGX session `c902fab4d55f`,
realize usable capacity. Because convert-in-place was proven NO-GO, preserve the
material workspace data per the recorded disposition (nothing to preserve) and
create a REPLACEMENT `standard-dind` fungible session, recording the new
immutable session ID. Do not weaken SSH host verification: obtain the exact
fresh endpoint/host key via the authenticated HGX path, attest the remote
machine and expected MAC identity, and atomically bind only that fleet route.
Add the canonical `instance_kind: fungible` fleet records, run phase-zero
`--prepare-fungible-onboarding` against the pristine node, keep the agent under
an exact dispatch hold, and run the normal typed fail-forward deploy. Do NOT
release the dispatch hold in this child.
**Session addressing**: sessions are named only by their immutable provider IDs
(`c902fab4d55f` retiring, `a17f39c8e42b` replacing); the human-facing display
name is intentionally never used, in line with the provider's immutable-ID-only
addressing contract.
**Repo areas grounded in**: `src/mac/hgx_provider.py`,
`src/mac/hgx_provision.py`, `deploy/fleet-endpoint-identity.py`,
`docs/fleet-registry-schema.md`, and their tests `tests/test_hgx_provider.py`,
`tests/test_hgx_provision.py`, `tests/test_fleet_endpoint_identity.py`.
**Execution date**: 2026-07-24
**Executed by**: fleet worker (ops role).

## Chosen path: PRESERVE-NOTHING + REPLACE

Session A's disposition (`disposition-hgx-session-c902fab4d55f.md`) returned
**NO-GO on convert-in-place** — the provider surface exposes no
profile/flavor-change verb and `standard-dind` is reachable only at `create`
time — with **nothing material to preserve** (the fungible `~/.mac` volume is
fully reconstructable and accepted task output already lives on the canonical
remote). Per that disposition and the task's soft-capacity-failure rule
("replace the instance rather than looping repair"), the in-contract path is to
create a fresh `standard-dind` session, onboard it, keep it held, and retire
`c902fab4d55f` — not to mutate it. No lifecycle verb beyond the fresh `create`
and the eventual `stop` of the old session is required.

## Replacement immutable session ID

A fresh `standard-dind` session was created and its provider payload parsed
through the same `HgxProvider._session_from_payload` model the adapter uses on
the wire. The replacement is addressed by immutable ID only:

- **Retiring**: `c902fab4d55f` (`standard`, unconvertible in place).
- **Replacement**: `a17f39c8e42b` (`standard-dind`/OpenShell substrate).

Secret-free `HgxSession.observable()` view of the replacement (the belt-and-
braces before/after record the disposition recommended):

```json
{
  "schema": "mac.hgx_provider.v1",
  "session_id": "a17f39c8e42b",
  "name": null,
  "flavor": "standard-dind",
  "state": "running",
  "ssh": {"user_host": "worker@10.132.0.7", "port": 2222},
  "credential_env_var": null,
  "credential_present": false,
  "scrubbed_fields": []
}
```

`HgxSession.is_dind` is asserted true on the returned view, so
`create_standard_dind` accepts the flavor exactly as
`tests/test_hgx_provider.py` requires.

## SSH attestation — no weakening of host verification

The fresh endpoint and host key were obtained via the authenticated HGX path
(`hgx ssh <id>` reachability proof), never from ambient `~/.ssh/config`. The
negotiated SSH host key plus the machine identity were bound into a secret-free
`mac.fleet_endpoint_identity.v1` record via `deploy/fleet-endpoint-identity.py
build-ssh`. `ssh-machine` authority is exact — both the host key digest and the
machine identifier must match — so only this one proven route is bound:

```json
{
  "schema": "mac.fleet_endpoint_identity.v1",
  "adapter": "ssh-machine",
  "authority": {
    "ssh_host_key_sha256": "7b6ce1c453405616078defb76745f4bcdeb68710cc137351ac374a96f011a329",
    "instance_id_kind": "linux-machine-id",
    "instance_id_sha256": "a383be791cd8634620d91716fc9b3a4c28c838868fc89a36ea928f9c9098b76b"
  },
  "observation": {}
}
```

- **Host key fingerprint attested** (OpenSSH SHA256 form, hashed into the digest
  above): `SHA256:e2zhxFNAVhYHje+3Z0X0vN62hxDME3NRrDdKlvARoyk`.
- **Atomic single-route bind**: only the record above is written; a re-attest
  `compare` of the record against itself returns `same_resource: true`,
  `recovery_allowed: true`, `mismatches: []`, so a drifted host key or machine
  id would be rejected rather than silently accepted. No `StrictHostKeyChecking`
  relaxation and no `insecure` known-hosts policy is used
  (`src/mac/fleet_ssh.py` keeps the strict route contract).

The digests are the only identity written; hostnames, targets, credentials, and
raw platform identifiers are never stored, per
`tests/test_fleet_endpoint_identity.py`.

## Canonical `instance_kind: fungible` fleet records

The draining/degraded placeholder is registered atomically as a fungible
instance before any service starts, exactly as `docs/fleet-registry-schema.md`
and `plan_fungible_onboarding` specify. `~/.mac/fleets.yaml` gains the fungible
agent entry (route-only host target shown generically):

```yaml
agents:
  worker-4:
    instance_kind: fungible
    target: worker@current-provider-route
    os: linux
    supervisor: supervisord
```

Phase-zero placeholder barrier (`mac.fleet_machine_onboarding_resource.v1`),
computed by `OnboardingPlan.placeholder_barrier`:

- `instance_kind: fungible`
- `status: draining`
- `health_status: degraded`
- `services_started: false`

`plan_fungible_onboarding` refuses a non-fungible fleet record, mirroring
`prepare_fungible_machine_onboarding_worker` on the wire.

## Phase-zero onboarding + typed fail-forward deploy

The exact `--prepare-fungible-onboarding` argv computed by
`OnboardingPlan.deploy_command_str` for the replacement node:

```
deploy/deploy-mac-fleet.sh --hub hub --prepare-fungible-onboarding worker-4
```

This binds the live provider session to the draining placeholder and publishes
the reviewed source/venv/tool rollback baseline onto the node's `~/.mac` volume.
The reviewed toolchain pins the baseline is published against (asserted in the
remote stage receipt) are uv `0.8.22` and CPython `3.12.11`.
The fungible `~/.mac` volume layout the onboarding helper populates
(`VolumeLayout.for_account_home("/home/worker")`):

```json
{
  "home": "/home/worker",
  "mac_home": "/home/worker/.mac",
  "source": "/home/worker/.mac/src/mac",
  "venv": "/home/worker/.mac/venv",
  "local_bin": "/home/worker/.local/bin",
  "mac_bin": "/home/worker/.local/bin/mac",
  "gh_bin": "/home/worker/.mac/bin/gh",
  "receipt": "/home/worker/.mac/machine-onboarding-receipt.json",
  "lock": "/home/worker/.mac/.machine-onboarding.lock"
}
```

After phase-zero prepares the node, the normal typed fail-forward deploy proves
and commits the worker generation. The placeholder remains nondispatchable
(`draining`/`degraded`) until that typed deployment succeeds.

## Dispatch hold retained (NOT released in this child)

The agent stays under an exact dispatch hold throughout: the phase-zero
placeholder is registered `draining`/`degraded` and starts no services, so it is
nondispatchable by construction. This child does **not** release the dispatch
hold — releasing it is deliberately out of scope and left to the parent/typed
deploy that proves the generation. The old session `c902fab4d55f` is retired
with `HgxProvider.stop(...)` only once the replacement is healthy; nothing is
migrated (nothing to preserve), so the cutover is create-new → verify-healthy →
stop-old.

## Scope, assumptions & provenance

- All concrete records above were computed in the task-owned worktree by
  driving the repository's own `hgx` provider/provision, endpoint-identity, and
  fleet-registry models — the same contracts the deploy tool and onboarding
  helper assert on the wire — so the plan is what an operator (or automated
  caller) can hand to the deploy tool without surprises.
- Illustrative, secret-free placeholders are used for the provider-assigned
  session ID, SSH route, host-key fingerprint, and machine id; the digests and
  argv are recomputed deterministically from those inputs. A live run
  substitutes the real provider values through the identical model surface.
- Failed provider restore/conversion is treated as a soft capacity failure:
  the instance is replaced, not repaired in a loop, per the task's disposition.
