!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Preflight: HGX auth path, fleet baseline, and standard-dind fungible reference

**Task**: `task_02966564e83c480cae92c1e68c5a4208` — session-agnostic groundwork
for converting two running HGX sessions into dispatchable MAC fungible workers.
**Parent task**: `task_823c1c41215d42d6aeef85da810f37bd` (convert HGX A/B into
dispatchable standard-dind fungible workers).
**Plan node**: `preflight`
**Scope**: bounded operational conversion groundwork. This is not a new
autoscaler and does not revive any cancelled surge-controller work.
**Safety invariant**: no running session is stopped, deleted, replaced, or
mutated by this preflight; no hub or fleet record is written. The record below
is planning evidence only.

## Immutable session identifiers

The two candidate sessions are addressed **only** by their immutable provider
session IDs. Human-facing pod/display names are never a safe selector and are
deliberately omitted here.

| Role in parent task | Immutable session ID |
|---------------------|----------------------|
| Session A           | `c902fab4d55f`       |
| Session B           | `c0b2f9fd4e0b`       |

`mac.hgx_provider.HgxProvider.resolve_session_id` refuses a display name that
maps to zero or multiple sessions and only proceeds when it resolves to exactly
one unique ID; an exact session-ID match is treated as unambiguous even when a
name collides. Every lifecycle verb below takes the immutable ID directly.

## 1. Authenticated HGX control path

The provider CLI is wrapped by `src/mac/hgx_provider.py`
(`HGX_PROVIDER_SCHEMA = "mac.hgx_provider.v1"`). All verbs shell out with a
fixed argv (never through a shell), request JSON where supported, and return
secret-free dataclasses. The authenticated control operations this preflight
relies on:

| Purpose | Adapter call | Underlying argv |
|---------|--------------|-----------------|
| Enumerate provider inventory | `HgxProvider.list()` | `hgx list --json` |
| Prove one session's state by immutable ID | `HgxProvider.status(session_id)` | `hgx status --json <id>` |
| Prove SSH reachability / endpoint by immutable ID | `HgxProvider.ssh_target(session_id)` | `hgx ssh --print <id>` (falls back to `status` for a structured target) |

Task-facing shape of the requested probes, addressed by immutable ID only:

- `hgx list` — establishes that `c902fab4d55f` / `c0b2f9fd4e0b` exist in
  provider inventory.
- `hgx ssh <immutable-id> -- hostname` — proves the SSH endpoint for that exact
  immutable session is actually reachable and runs a command; the adapter's
  `ssh_target` parses/validates the endpoint through
  `mac.fleet_deploy.parse_ssh_target` so downstream code always receives a
  validated `SshTarget` (host / user / port, never a credential).
- Endpoint / host-key retrieval — the SSH target is parsed from `hgx ssh
  --print` output (or a structured `status` payload). The strict host-key pin is
  re-attested through the provider before a replacement key is trusted (see
  `docs/book/04-machines-and-agents.md`).

Hard authentication guardrails enforced by the adapter:

- **`hgx info` is banned outright** (`_FORBIDDEN_VERB = "info"`). It can echo a
  fallback bootstrap password on stdout, so `_run` raises before executing it.
- **No credential is ever stored or observed.** Secret-hinting payload fields
  (`password`, `token`, `private_key`, …) are scrubbed; only a
  `credential_env_var` *name* plus a `credential_present` flag are exposed, and
  provider stderr is captured for diagnostics but never copied into any
  observable/persisted structure.
- **Immutable-ID addressing only** for `status`, `ssh`, `stop`, `resume`.

## 2. Fleet ground truth for both immutable IDs

Baseline confirmation (session-agnostic; recorded as the state a per-session
child must not assume away). For each immutable ID `c902fab4d55f` and
`c0b2f9fd4e0b`, the preflight expectation is that **none** of the following
exist yet:

- **No fleet-registry entry** — neither ID appears as an agent in the fleet
  registry (`docs/fleet-registry-schema.md`), so neither carries
  `instance_kind`, target, or supervisor metadata.
- **No hub agent/machine registration** — no `machine_*` record and no agent
  identity are registered against the hub for either session.
- **No MAC home / CLI / source** — the node has no populated `~/.mac` volume:
  `~/.mac/src/mac`, `~/.mac/venv`, `~/.local/bin/mac`, `~/.mac/bin/codegraph`,
  and `~/.mac/bin/gh` are absent, matching the pristine layout in
  `VolumeLayout.for_account_home` (`src/mac/hgx_provision.py`) and
  `Layout` in `deploy/fleet-node-machine-onboard.py`.
- **No deployed generation** — no `~/.mac/deployed-source-revision`, no worker
  generation in `~/.mac/mac.env`, and no committed
  `~/.mac/machine-onboarding-receipt.json`.
- **No Docker/OpenShell executor substrate** — no MAC service configuration and
  no running MAC service processes; the OpenShell (docker-in-docker) executor
  is not yet installed. This is exactly the pristine set asserted by
  `validate_pristine` in `deploy/fleet-node-machine-onboard.py`
  (`source_absent`, `venv_absent`, `deployed_revision_absent`,
  `worker_generation_absent`, `service_configuration_absent`,
  `service_process_absent`).

**Assumption**: this preflight documents the *expected* baseline and the exact
probes that confirm it; it does not itself write to the hub or provider. A
per-session child re-proves the baseline for its single immutable ID immediately
before mutation, because phase-zero eligibility is re-checked on the wire.

## 3. Canonical standard-dind fungible reference

The reference used by existing dispatchable HGX workers, as encoded in the
repository:

- **Executor substrate / flavor**: `standard-dind`
  (`STANDARD_DIND_FLAVOR = "standard-dind"` in `src/mac/hgx_provider.py`). This
  is the flavor required for OpenShell / Docker (docker-in-docker) execution and
  is created through the first-class `HgxProvider.create_standard_dind(...)`
  (argv `hgx create --flavor standard-dind --json`), which rejects any provider
  response whose flavor is not `standard-dind`.
- **Profile / CPU / memory**: the `standard-dind` profile (vCPU and memory) is
  defined provider-side by the named flavor; the repository intentionally
  addresses it only by the flavor constant and does not hard-code a sizing
  number, so the canonical sizing is "whatever the provider's `standard-dind`
  flavor allocates". Any concrete CPU/memory target lives outside git in the
  fleet spec, not in this note.
- **Reviewed runtime toolchain pins** (published onto the node's `~/.mac`
  baseline; `src/mac/hgx_provision.py` and `deploy/fleet-node-machine-onboard.py`
  must agree on these): `uv 0.8.22`, CPython `3.12.11`, CodeGraph `v1.1.6`.
- **`instance_kind: fungible` record shape**: the phase-zero placeholder is
  registered atomically as `instance_kind=fungible`, `status=draining`,
  `health_status=degraded`, with a `machine_onboarding` resource
  (`schema = mac.fleet_machine_onboarding_resource.v1`, `status=prepared`) on
  both the machine and agent records, `trusted=true`, and **no services
  started**. This mirrors `register_fungible_onboarding_placeholder` in
  `deploy/deploy-mac-fleet.sh` and the plan emitted by
  `plan_fungible_onboarding` (`OnboardingPlan.as_dict`,
  `schema = mac.hgx_fungible_onboarding_plan.v1`, `services_started: false`).
  The registry form is shown in `docs/fleet-registry-schema.md` and
  `docs/book/04-machines-and-agents.md`.
- **Phase-zero `--prepare-fungible-onboarding`**: the exact deploy argv for one
  agent is
  `deploy/deploy-mac-fleet.sh --hub <hub-node> --prepare-fungible-onboarding <agent>`
  (rendered by `OnboardingPlan.deploy_command()` /
  `deploy_command_str()`). It binds the live SSH-machine route, accepts only a
  pristine or failed-prephase node (source and venv absent, no deployed revision
  or MAC services), installs the exact reviewed `uv`/Python/CodeGraph baseline
  plus the frozen source archive, registers the agent atomically as
  draining/degraded/fungible, and **starts no services**.
- **Typed fail-forward deploy command**: after phase-zero, the *normal* typed
  cohort deployment runs separately —
  `deploy/deploy-mac-fleet.sh --hub <hub-node> <agent>` — to prove and commit
  the worker generation. Incomplete deployments retain their newest on-node
  state, diagnostic bundle, dispatch hold, and process barrier by default so
  repair rolls **forward** in place; `--recovery-policy rollback` is an explicit
  break-glass override.
- **Dispatch-hold mechanism**: the deploy transaction manages a hub dispatch
  hold through the typed CAS routes
  (`/agents/{agent_id}/dispatch-hold/acquire`,
  `/agents/{agent_id}/dispatch-hold/release`,
  `/agents/dispatch-hold/release-batch`, and the successor
  `/agents/dispatch-hold/transition-batch`). The fungible placeholder is
  nondispatchable while `status=draining`/`health_status=degraded`; the worker
  only becomes dispatchable after the typed deployment clears the hold on exact
  full-cohort reconciliation.

## 4. Per-session child release-gate checklist

A per-session child (one immutable ID) must satisfy all of the following before
its worker is released for dispatch:

- [ ] The target is addressed **only** by its immutable session ID
      (`c902fab4d55f` or `c0b2f9fd4e0b`); no display-name selection.
- [ ] `hgx list` shows the immutable ID in provider inventory and
      `hgx ssh <immutable-id> -- hostname` proves SSH reachability; the endpoint
      and host-key pin are captured via the adapter (never via `hgx info`).
- [ ] A fleet-registry entry exists with `instance_kind: fungible` for the
      agent bound to that session (phase-zero refuses a non-fungible record).
- [ ] Node eligibility proven pristine/failed-prephase: source absent, venv
      absent, deployed revision absent, worker generation absent, committed
      onboarding receipt absent, service configuration absent, service process
      absent (`validate_pristine`), and a complete SSH-machine route identity
      (`ssh_host_key_sha256` + `instance_id_sha256`) is bound.
- [ ] Phase-zero `--prepare-fungible-onboarding` installs the exact reviewed
      `uv 0.8.22` / Python `3.12.11` / CodeGraph `v1.1.6` baseline plus the
      frozen source archive, and registers the placeholder atomically as
      `instance_kind=fungible`, `status=draining`, `health_status=degraded`,
      `services_started=false`.
- [ ] The subsequent **normal typed** deployment (`--hub <hub-node> <agent>`,
      no phase-zero flag) proves and commits the worker generation; recovery is
      fail-forward by default.
- [ ] The dispatch hold is released only on exact full-cohort reconciliation,
      after which the worker reports dispatchable.
- [ ] The **other** running session and every unrelated fleet member are left
      untouched throughout (no stop/resume/replace, no cross-session mutation).

## Assumptions recorded

- Sessions are identified strictly by immutable ID; display/pod names are
  intentionally excluded from this checked-in note (they are operator identity
  that lives outside git).
- Concrete `standard-dind` CPU/memory sizing and the concrete hub node name are
  provider/fleet-spec facts kept outside the repository; this note pins only the
  repository-encoded flavor constant, toolchain versions, schemas, commands, and
  gate order.
- This is a `repo_change` preflight whose deliverable is this planning record;
  no live session, hub, or fleet-registry state was mutated to produce it.
