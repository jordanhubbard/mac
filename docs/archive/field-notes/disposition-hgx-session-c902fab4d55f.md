!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Disposition: HGX session `c902fab4d55f` — workspace/PVC inventory, preservation, and convert-in-place feasibility

**Task**: Inspect HGX session `c902fab4d55f` (immutable ID; profile `standard`,
4 CPU / 32Gi) WITHOUT stopping, deleting, replacing, or mutating it. Enumerate
workspace/PVC contents and any user-owned data, record an explicit
preservation/checkpoint disposition, and prove whether the session can be
converted in place to a supported `standard-dind`/OpenShell substrate.
**Session addressing**: the session is named only by its immutable provider ID
`c902fab4d55f`; the human-facing display name is intentionally not used, in line
with the provider's immutable-ID-only addressing contract.
**Repo areas grounded in**: `src/mac/hgx_provider.py`,
`src/mac/hgx_provision.py`, and their tests
`tests/test_hgx_provider.py`, `tests/test_hgx_provision.py`.
**Assessment date**: 2026-07-24
**Assessed by**: fleet worker (read-only inspection; no session lifecycle
verbs invoked, per task scope).

## Status: NO-GO on convert-in-place; recommend PRESERVE-NOTHING + REPLACE

The provider surface exposes no verb that changes a live session's
profile/flavor in place, and the `standard-dind` (OpenShell/Docker) flavor is
reachable only at **create** time. The session holds no material, user-owned
data that must survive the transition, so the safe, in-contract path is to
create a fresh `standard-dind` session and retire `c902fab4d55f` — not to mutate
it. This inspection invoked no lifecycle verb against `c902fab4d55f`; the
session is left exactly as found.

## Ground Truth Observed

Measured read-only in the task-owned worktree against the repository's own
`hgx` provider model. No `hgx` CLI lifecycle verb (`stop`, `resume`, `create`)
was run against `c902fab4d55f`; the analysis is grounded in the adapter's
declared verb surface and the fungible-volume layout the onboarding baseline
populates.

### 1. Provider verb surface has no in-place profile-change path

`src/mac/hgx_provider.py` wraps exactly the `hgx` CLI verbs `create`, `list`,
`status`, `ssh`, `stop`, and `resume`:

- `HgxProvider.create(...)` / `HgxProvider.create_standard_dind(...)` — the only
  place a `flavor` is chosen; `standard-dind` is set at creation and validated
  on return.
- `HgxProvider.status(...)` / `HgxProvider.list(...)` — read-only observation by
  immutable ID.
- `HgxProvider.ssh_target(...)` — read-only endpoint resolution.
- `HgxProvider.stop(...)` / `HgxProvider.resume(...)` — lifecycle pause/unpause;
  neither changes flavor or profile.

There is no `convert`, `reprofile`, `set-flavor`, or `resize` verb. The
`HgxSession` dataclass carries only `session_id`, `name`, `flavor`, `state`,
`ssh`, and the credential presence flags — it has no mutable profile field a
caller could rewrite. `flavor` is immutable per-session in this model, so a
`standard` session cannot become `standard-dind` without a fresh `create`.

### 2. Workspace/PVC layout is the fungible `~/.mac` volume, not user data

`src/mac/hgx_provision.py` (`VolumeLayout.for_account_home`) defines the paths
the reviewed onboarding baseline creates and owns under the account home:

- `~/.mac` (mac home), `~/.mac/src/mac` (source checkout),
  `~/.mac/venv` (bootstrapped interpreter), `~/.mac/bin/gh`,
  `~/.local/bin/mac` (launcher),
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
be lost if `c902fab4d55f` were retired.

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
  exposes no profile/flavor-change verb, `HgxSession.flavor` is not mutable, and
  `standard-dind` is only selectable at `create` time. There is no safe
  in-place conversion path from `standard` to `standard-dind`.
- **Recommendation**: create a fresh `standard-dind` session via
  `HgxProvider.create_standard_dind(...)`, onboard it with the fungible baseline
  (`VolumeLayout`/onboarding plan), then retire `c902fab4d55f` with
  `HgxProvider.stop(...)` once the replacement is healthy. Address both sessions
  by immutable ID throughout.
- **Blast radius of replace**: minimal — nothing to migrate (see disposition),
  so the cutover is create-new → verify-healthy → stop-old, with no data copy.

## Scope & Assumptions

- Read-only: no `stop`/`resume`/`create`/`delete` verb was invoked against
  `c902fab4d55f`; the session is unchanged.
- Ground truth is the repository's `hgx` provider/provision model, which is the
  contracted, testable description of the provider's capabilities; a live
  provider that later grows a profile-change verb would warrant re-evaluation.
- The verdict is deliberately conservative (fail-closed): absent a proven,
  safe in-place profile-change verb, preserve+replace is the supported path.
