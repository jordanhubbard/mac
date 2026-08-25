# Developer Guide

How to work on mac itself. The conventions here are not stylistic preferences —
each one exists because its absence caused a specific, recoverable-but-expensive
incident.

## Set up

```console
eval "$(scripts/start-test-postgres.sh)"   # exports MAC_TEST_PG_URL
uv run --extra dev pytest -q -n 8          # full suite, ~10 min
```

The suite runs against **PostgreSQL, not SQLite**, because that is what the
fleet runs. Without `MAC_TEST_PG_URL` the suite fails fast with instructions
rather than skipping — a suite that silently covers nothing is worse than a red
one.

`start-test-postgres.sh` sets `max_locks_per_transaction=1024`. Each test gets
its own schema and applying the DDL takes one lock per object in a single
transaction; at Postgres' default of 64 a parallel run fails with "out of
shared memory", which looks nothing like a test failure.

## Always work in a worktree

```console
git -C ~/Src/mac worktree add /tmp/mac-<task> -b <you>/<task>
cd /tmp/mac-<task>
```

This is mandatory, not advisory. Multiple agents run against this repository
concurrently. Two sharing the main checkout collided twice in one day: one
`git add -A` nearly swept ~1,200 lines of another's half-finished work into an
unrelated commit, and a `git commit -a` actually did — landing someone else's
retry implementation inside a commit titled for something else. Both failures
are silent; the tests pass and the damage is only visible later in `git log`.

Remove it when the work has landed:

```console
git -C ~/Src/mac worktree remove /tmp/mac-<task>
```

If you must use the main checkout, never `git add -A`, `git add .` or
`git commit -a`. Stage explicit paths.

## The gates

`sanity` is the one that counts — roughly 11,000 tests, 40–55 minutes. Others
are fast and worth running locally first:

```console
scripts/run-contract-tests.sh            # the fail-fast preflight
bash scripts/dead-code-check.sh          # vulture, ≥90% confidence
uv run python scripts/generate-env-config-registry.py --check
uv run python scripts/generate-docs-reference.py --check
uv run python scripts/test-docs.py --static-only
npm --prefix observe test                # console
npm --prefix ide test                    # Fleet IDE typecheck
npx playwright test                      # Fleet IDE browser tests (~9s)
```

**Run the browser tests.** `tsc --noEmit` passing is not the same as the UI
working: deleting a rendered element typechecks perfectly. A missed Playwright
spec has cost a CI cycle more than once, and it runs in nine seconds.

## Test selection, and the traps in it

`sanity` does not always run everything. `scripts/resolve-impacted-tests.py`
selects tests from a committed impact map and fails **closed** to a full run
when it cannot map a change.

Two things to know:

- **The impact map interns node ids and references them by integer index.**
  Deleting or renaming a test strands ids. There is a gate for it, and it now
  compares against actual `pytest --collect-only` output rather than function
  names — because an empty `@pytest.mark.parametrize` list produces **zero**
  tests while the function still exists, which a name-based check cannot see.
  That left 180 stale ids that failed unrelated PRs with a pytest *usage*
  error.
- **Changing the selector forces a full run.** `test-policy.toml`,
  `select-sanity-tests.py`, `resolve-impacted-tests.py`,
  `build-test-impact-map.py` and `src/mac/test_checkpoint.py` are all in
  `global_full_paths`, so the component that decides what gets verified cannot
  narrow its own verification.

## Writing tests that are worth having

The house style, and the reasoning:

- **A test must fail without the change.** Verify it: revert the source, run
  the test, confirm red. A test that passes both ways documents nothing.
- **Assert the property, not the incident.** Pin what must be true, not the
  shape of one bug.
- **Say why in the docstring.** The best tests in this repository open with the
  failure that motivated them, including the real error text. That is what
  stops someone "simplifying" the test later.
- **Test the layer that ships.** The CLI's default view broke because its tests
  drove `ControlPlane.list_tasks` directly while the bug was in the HTTP route
  signature. If a client talks over HTTP, test over HTTP.
- **Beware tests that pin an absence.** Several tests asserted a feature was
  *not* present, purely because a constant disabled it. When the feature
  arrived they read as intent. Prefer asserting what *is* allowed.

## Client and API stay in step

`RemoteDispatch` is the single seam every hub-mode client crosses.
`tests/test_dispatch_route_contract.py` extracts all ~260 call sites and
asserts each resolves against the live FastAPI route table, and that every
query parameter sent is declared. It found a real bug on its first run: the CLI
sent `eval_set` where the route declared `eval_set_id`, and FastAPI drops an
undeclared parameter *silently* — the request succeeded and the filter did
nothing.

An unresolvable call site **fails** that gate rather than being skipped.
Coverage that shrinks quietly is the failure the equivalent Fleet IDE canary
was rewritten to fix.

## Schema changes

`src/mac/data/postgres/schema.sql` is applied at hub start and is
`CREATE TABLE IF NOT EXISTS` throughout.

- A **new table** provisions itself on restart.
- A **new column on an existing table does not.** It works on a fresh database
  and in CI (fresh schema per test) and silently does nothing on a long-lived
  hub. Hand-apply the `ALTER`, and diff `schema.sql` across the deploy range
  before deploying.
- `migrations/` is **manual only**. Nothing in mac reads it, and it is not
  shipped in the wheel.

## Deploying the hub

The hub serves from `~/.mac/src/mac` on the hub host — **not** a developer
checkout.

```console
git -C ~/.mac/src/mac fetch origin && git -C ~/.mac/src/mac merge --ff-only origin/main
sudo launchctl kickstart -k system/com.mac.control-plane
```

Verify by **PID change**, not checkout SHA: `refresh-source` has reported
`restart_requested: false` while the checkout advanced. `/health` returns empty
for ~10s during startup; that is not a failure.

After a manual pull on the hub host, also restart the co-located agent
(`com.mac.agent`) — it shares that checkout and will otherwise keep running
stale code through several refreshes.

## Debugging the fleet

```console
mac admin fleet doctor
mac task why-unclaimed <id>
mac admin observability list --name executor.agent_completed
mac agent list --json                     # hardware under resources.hardware
```

Read the hub's own log at `~/.mac/logs/mac-service.log`. Watch for
best-effort `except Exception` blocks: one swallowed a `NameError` and
presented it as a `None` return, which cost an hour of misdirected debugging.
`--log-cli-level=WARNING` surfaces those in tests.
