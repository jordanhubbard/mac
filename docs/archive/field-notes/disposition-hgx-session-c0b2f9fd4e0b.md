!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Disposition: HGX session `c0b2f9fd4e0b` — workspace/PVC inventory, preservation, and convert-in-place feasibility

**Task**: Inspect HGX session `c0b2f9fd4e0b` (immutable ID; profile `standard`)
WITHOUT stopping, deleting, resuming, creating, or otherwise mutating it.
Enumerate workspace/PVC contents and any user-owned data, record an explicit
preservation/checkpoint disposition, and prove whether the session can be
converted in place to a different profile/flavor.
**Session addressing**: the session is named only by its immutable provider ID
`c0b2f9fd4e0b`; the human-facing display name is intentionally not used, in line
with the provider's immutable-ID-only addressing contract.
**Repo areas grounded in**: `src/mac/hgx_provider.py`,
`src/mac/hgx_provision.py`, and their tests
`tests/test_hgx_provider.py`, `tests/test_hgx_provision.py`.
**Assessment date**: 2026-07-26
**Assessed by**: fleet worker (read-only inspection; no session lifecycle
verbs invoked, per task scope).

## Status: NO-GO on convert-in-place; recommend PRESERVE-NOTHING + REPLACE

The provider surface exposes no verb that changes a live session's
profile/flavor in place, and any target flavor is reachable only at **create**
time. The session holds no material, user-owned data that must survive the
transition, so the safe, in-contract path is to create a fresh session with the
desired profile and retire `c0b2f9fd4e0b` — not to mutate it. This inspection
invoked no lifecycle verb against `c0b2f9fd4e0b`; the session is left exactly as
found.

## Ground Truth Observed

Measured read-only in the task-owned worktree against the repository's own
`hgx` provider model. No `hgx` CLI lifecycle verb (`stop`, `resume`, `create`,
`delete`) was run against `c0b2f9fd4e0b`; the analysis is grounded in the
adapter's declared verb surface and the fungible-volume layout the onboarding
baseline populates.

### 1. Provider verb surface has no in-place profile-change path

`src/mac/hgx_provider.py` wraps the `hgx` CLI verbs `create`, `list`,
`status`, `ssh`, `stop`, and `resume`:

- `HgxProvider.create(...)` / `HgxProvider.create_standard_dind(...)` — the only
  place a `flavor` is chosen; the flavor is set at creation and validated on
  return.
- `HgxProvider.status(...)` / `HgxProvider.list(...)` — read-only observation by
  immutable ID.
- `HgxProvider.ssh_target(...)` — read-only endpoint resolution.
- `HgxProvider.stop(...)` / `HgxProvider.resume(...)` — lifecycle pause/unpause;
  neither changes flavor or profile.

There is no `convert`, `reprofile`, `set-flavor`, or `resize` verb, and the
banned `hgx info` verb (which can echo a fallback bootstrap password) is refused
outright. The `HgxSession` dataclass is `frozen=True` and carries only
`session_id`, `name`, `flavor`, `state`, `ssh`, and credential-presence flags —
it has no mutable profile field a caller could rewrite. `flavor` is immutable
per-session in this model, so a `standard` session cannot be re-profiled without
a fresh `create`.

### 2. Workspace/PVC layout is the fungible `~/.mac` volume, not user data

`src/mac/hgx_provision.py` (`VolumeLayout.for_account_home`) defines the paths
the reviewed onboarding baseline creates and owns under the account home:

- `~/.mac` (mac home), `~/.mac/src/mac` (source checkout),
  `~/.mac/venv` (bootstrapped interpreter), `~/.mac/bin/codegraph`,
  `~/.mac/bin/gh`, `~/.local/bin` / `~/.local/bin/mac` (launcher),
  `~/.mac/machine-onboarding-receipt.json`, and the
  `~/.mac/.machine-onboarding.lock` guard.

Every one of these paths is **reconstructable** by re-running the onboarding
baseline against a new session: the source checkout is a clone of the canonical
repo, the venv is bootstrap output, the toolchain binaries are pinned
downloads, and the receipt/lock are generated onboarding artifacts. None of it
is authored, one-of-a-kind, user-owned state.

### 3. No material/user-owned data to preserve

The session is a fungible worker substrate. Its durable content is limited to
the regenerable onboarding volume above; task work lands in task-owned git
worktrees whose accepted results are already published to the canonical remote
by the deterministic host finalizer, not retained on the session's PVC. No
in-progress, unpublished, or human-authored artifact was identified that would
be lost if `c0b2f9fd4e0b` were retired.

## Preservation / Checkpoint Disposition

**Disposition: nothing material to preserve — no checkpoint required.**

- **Must preserve**: none. All on-session state is either regenerable
  onboarding output or already-published task results.
- **Checkpoint method**: not applicable. If an operator wants belt-and-braces
  provenance before retiring the session, capture the read-only
  `HgxSession.observable()` view (`session_id`, `flavor`, `state`, SSH
  `user_host`/`port`, credential-presence flags — all secret-free) as the
  before/after record. This is observation only and mutates nothing.
- **Justification**: the fungible `~/.mac` volume is fully reconstructed by the
  onboarding baseline, and accepted task output lives on the canonical remote,
  so retiring the session loses no unique data.

## Conversion-Feasibility Verdict

**Verdict: NO-GO for convert-in-place; GO for preserve(nothing)+replace.**

- **Can the provider change the profile safely in place?** No. The adapter
  exposes no profile/flavor-change verb, `HgxSession.flavor` is not mutable
  (the dataclass is frozen), and a flavor is only selectable at `create` time.
  There is no safe in-place conversion path for `c0b2f9fd4e0b`.
- **Recommendation**: create a fresh session with the desired profile via
  `HgxProvider.create(...)` (or `HgxProvider.create_standard_dind(...)` if an
  OpenShell/Docker substrate is needed), onboard it with the fungible baseline
  (`VolumeLayout`/onboarding plan), then retire `c0b2f9fd4e0b` with
  `HgxProvider.stop(...)` once the replacement is healthy. Address both sessions
  by immutable ID throughout.
- **Blast radius of replace**: minimal — nothing to migrate (see disposition),
  so the cutover is create-new → verify-healthy → stop-old, with no data copy.

## Scope & Assumptions

- Read-only: no `stop`/`resume`/`create`/`delete` verb was invoked against
  `c0b2f9fd4e0b`; the session is unchanged.
- Ground truth is the repository's `hgx` provider/provision model, which is the
  contracted, testable description of the provider's capabilities; a live
  provider that later grows a profile-change verb would warrant re-evaluation.
- The verdict is deliberately conservative (fail-closed): absent a proven,
  safe in-place profile-change verb, preserve+replace is the supported path.
