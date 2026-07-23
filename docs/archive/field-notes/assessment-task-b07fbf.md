!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Assessment: task_b07fbff6994e41a39ce24157f1832ad5

**Task**: Establish ground truth for the parent task "Confirm/harden
fleet-scoped worker token precedence fix (EX_TEMPFAIL 75 root cause)". Read-only
investigation; no source edits.
**Assessment date**: 2026-07-23
**Assessed by**: fleet worker (documented audit; no source or test edits).

This note records the reviewed audit findings so a follow-up hardening task has
an exact, file:line call-site list. All line numbers reference the repository
tip audited for this note; the fix has moved from the parent task's original
line hints (`worker.py` ~6280/6462) as the file grew, but the behavior is
unchanged.

## 1. Token precedence fix in `worker.main()` — CONFIRMED

- `src/mac/worker.py` `main()` defers token resolution: when `args.token is
  None` it calls `mac.fleet_env.resolve_first(["MAC_WORKER_TOKEN", "MAC_TOKEN",
  "MAC_API_TOKEN"], fleet=args.fleet)`. An explicit `--token` still wins because
  the resolver only runs when `args.token is None`.
- `resolve_first()` looks up each base name via `resolve()`, which tries the
  fleet-scoped `BASE__<FLEET>` form first (from `--fleet` or `MAC_FLEET`) and
  only then the legacy flat `BASE`. So a scoped `MAC_WORKER_TOKEN__<FLEET>` wins
  over a legacy flat `MAC_WORKER_TOKEN`.
- `build_parser()` documents the deferral in a comment: the `--token` default is
  left `None` on purpose so a fleet-blind flat token cannot be baked into the
  parser default and beat the scoped form (and so the mac-g55y deprecation
  warning does not fire at parse time). The `--fleet` argument defaults to
  `MAC_FLEET`.

## 2. EX_TEMPFAIL / exit-75 semantics — CONFIRMED

- `SELF_UPDATE_RESTART_EXIT_CODE = 75` (EX_TEMPFAIL from sysexits.h) is defined
  with a comment: `main()` returns it so the supervising service manager
  restarts the worker after a self-update swaps code on disk, instead of
  treating the exit as a hard failure.
- `main()` returns `75` **only** on `result.status == "self_update_restart"`
  (single-run path) or when any loop result has that status (`--loop` path).
  No other path returns 75.
- Root-cause narrative for the historical "EX_TEMPFAIL 75" surface: before the
  deferral fix, a machine in multiple fleets could pick up the wrong legacy flat
  `MAC_WORKER_TOKEN`/`MAC_API_TOKEN` (a fleet-blind default baked at parse time)
  instead of the correct scoped `MAC_*__<FLEET>`. The mis-resolved/empty token
  then failed hub auth/heartbeat. Under the service wrapper (which crash-loops
  and, on some paths, remaps recoverable transport/exit codes), that auth
  failure surfaced to the operator as a persistent EX_TEMPFAIL(75) restart
  loop rather than a clean auth error. Deferring resolution to `main()` once
  `--fleet` is known makes the scoped token win, so the worker authenticates
  and 75 is again reserved for genuine self-update restarts. `main()` also now
  wraps bare `TimeoutError`/`ConnectionError` as a recoverable error exit so a
  transient hub blip does not crash-loop the service under `set -e`.

## 3. Direct token-env call sites — reviewed classification

Enumerated every non-test read of `MAC_WORKER_TOKEN` / `MAC_TOKEN` /
`MAC_API_TOKEN` via `os.environ` / `getenv` / a mapping `.get()` that bypasses
`mac.fleet_env` fleet-scoped resolution.

### Hardening targets (client/worker credential resolution that SHOULD be fleet-scoped)

- `src/mac/cli.py:3467` — `_hub_get_mood()` reads
  `os.environ.get("MAC_WORKER_TOKEN") or os.environ.get("MAC_API_TOKEN")`
  directly to call the hub HTTP API from a client node. This is client-side hub
  credential resolution and should defer to `mac.fleet_env`
  (`resolve_first`/`resolve` with the node's effective fleet), matching
  `worker.main()` and `dispatch._resolve_hub_token`.
- `src/mac/openshell_collector.py:117` — the `--token` argparse default is
  `os.environ.get("MAC_WORKER_TOKEN") or os.environ.get("MAC_API_TOKEN")`. This
  is a worker-side collector authenticating to the hub; it should resolve
  fleet-scoped (ideally deferred like `worker.build_parser()`/`main()` so an
  explicit `--token` still wins and no flat token is baked at parse time).
- `src/mac/_hermes/tools/fleet_tool.py:48-50` — `_hub_env()` reads
  `MAC_WORKER_TOKEN`/`MAC_TOKEN`/`MAC_API_TOKEN` flat to talk to the hub. It is
  the worker/executor hub credential resolver ("mirrors the worker/executor
  resolution" per its own comment) and should be fleet-scoped for parity. Note
  this file lives under `src/mac/_hermes/`, which the docs-identity guard
  excludes; it is still in scope for credential hardening.

### Intentionally left flat (NOT fleet-scoped) — with rationale

- `src/mac/dispatch.py:2972` — `_resolve_hub_token()` already resolves
  `MAC_API_TOKEN` via `resolve_env_var(..., fleet=...)` (fleet-scoped). The bare
  `env.get("MAC_WORKER_TOKEN")` at line 2972 is only a **fallback** for K8s Job
  pods that carry `MAC_WORKER_TOKEN` injected by the runner. Pod-injected
  secrets are already fleet-specific by construction, so leaving the fallback
  flat is correct. No change needed.
- `src/mac/k8s/job_executor.py:53` and `:243` — read
  `MAC_WORKER_TOKEN`/`MAC_API_TOKEN` from the pod env injected by the K8s
  runner/Secret. Pod secret injection is per-Job and already scoped; leave flat.
- `src/mac/k8s/orchestrator.py:193` and `src/mac/k8s/bootstrap.py:581` — read
  the runner/bootstrap token from pod env / cluster Secret. Same rationale:
  in-cluster secret injection, leave flat.
- `src/mac/deploy_env.py:510,820,862,866,868` — read from the **rendered env
  `values` mapping** (deploy-time env-file rendering), not process env. This is
  the code that WRITES the per-node env file (including scoped variants via the
  migrate path), so it legitimately operates on flat keys. Leave flat.
- `src/mac/supervisor.py:131` — uses `MAC_SUPERVISOR_TOKEN` with an
  `MAC_API_TOKEN` fallback for the local ops channel; supervisor identity is
  node-local, not fleet-routed. Leave flat.
- `src/mac/_hermes/tools/embedding_tool.py:38` and `src/mac/eval_runner.py:610`
  — `MAC_API_TOKEN` is only a last-resort fallback behind gateway/router keys
  (`MAC_HERMES_GATEWAY_API_KEY`/`OPENAI_API_KEY`/`MAC_ROUTER_TOKEN`); these are
  gateway credentials, not the fleet hub worker token path. Leave flat.

## 4. Test coverage and gaps

Confirmed existing coverage:

- `tests/test_fleet_env.py` — covers `scoped_var` normalization/rejection,
  `resolve` scoped-over-legacy precedence, legacy fallback, `MAC_FLEET`-driven
  scoping, and `resolve_first` priority-chain walking. Solid for the resolver.
- `tests/test_worker_main_contract.py` — `test_worker_main_resolves_fleet_token_
  and_heartbeat` asserts `main()` uses the fleet-aware resolver; exit-75 is
  covered by the `self_update_restart` single/loop return tests and
  `test_self_update_restart_exit_code_is_ex_tempfail` /
  `test_self_update_restart_uses_the_named_constant`.
- `tests/test_worker.py` — asserts `build_parser()` leaves `args.token is None`
  so resolution defers to `main()`.

Gaps for the hardening targets (add when the hardening task lands):

- No test asserts `cli._hub_get_mood()` resolves a scoped
  `MAC_WORKER_TOKEN__<FLEET>` over a flat token.
- No test asserts `openshell_collector.main()`'s token default is fleet-scoped
  / deferred.
- No test asserts `_hermes/tools/fleet_tool._hub_env()` resolves scoped tokens.
- (Negative-side, optional) no test pins the K8s / deploy_env / supervisor reads
  as intentionally flat, so a future refactor could silently "fix" them.

## Determination

- The precedence fix and exit-75 semantics are present and correct as described.
- Hardening targets: `cli.py:3467`, `openshell_collector.py:117`,
  `_hermes/tools/fleet_tool.py:48-50`.
- Leave flat (with rationale above): `dispatch.py:2972` fallback, the K8s reads
  (`job_executor.py:53,243`, `orchestrator.py:193`, `bootstrap.py:581`),
  `deploy_env.py` rendered-values reads, `supervisor.py:131`, and the gateway
  fallbacks in `embedding_tool.py:38` / `eval_runner.py:610`.
- This audit changes no source or tests; it records ground truth only.
