# ADR 0025 - "Hermes" is a persona name, not a runtime

- Status: **Proposed**
- Date: 2026-08-20
- Decision owner: MAC fleet owner
- Related: ADR 0001 (unify the Hermes runtime into `mac`), ADR 0007 (per-module
  ownership: mood, nap, soul, memory), `docs/hermes-retirement-premises.md`,
  `docs/hermes-vendor-fate.md`

## Context

Every agent runs OpenClaw. The vendored Hermes runtime under `src/mac/_hermes`
is gone, and `gateway_ownership.services` reports `{hermes: inactive, nemoclaw:
inactive, openclaw: active}`. The obvious conclusion is that the five remaining
`hermes_*` modules — 5,565 lines — are dead code awaiting a delete.

That conclusion is wrong, and the cost of acting on it would have been an
outage: `build_hermes_startup_report()` is called at four points in `api.py`,
including `app.state.hermes_startup` on hub startup. Deleting it takes the hub
down.

The problem is that "hermes" in this tree names two different things:

1. **A retired vendor runtime.** Deleted with `src/mac/_hermes`.
2. **Live MAC plumbing that was merely written during the Hermes era and named
   after it.** Persona instances, soul/memory scoping, gateway config surface,
   startup diagnostics. None of it is Hermes-specific. All of it is on the
   live path today, under OpenClaw.

So the remaining work is a *rename*, not a delete — plus removal of whatever is
genuinely unreachable, which had to be measured rather than assumed.

## Measurement

`scripts/prove-module-reachability.py` computes, as a fixed point over
cross-module Python references, non-Python references (the fleet installers
embed Python in shell heredocs), and intra-module edges, which top-level symbols
of a module are never reached from a production entry point. Tests are
deliberately *not* a reference source: a symbol whose only caller is its own
test is dead code plus a dead test.

Measured 2026-08-20, before any change in this ADR:

| module | symbols | reachable | unreachable |
|---|---|---|---|
| `hermes_adapter.py` | 59 | 59 | — |
| `hermes_chat_config.py` | 15 | 15 | — |
| `hermes_config_surface.py` | 52 | 51 | `update_fleet_hermes_surface` |
| `hermes_runtime.py` | 20 | 20 | — |
| `hermes_startup.py` | 45 | 45 | — |

**190 of 191 top-level symbols are live.** The genuinely unreachable Hermes
runtime is one 36-line function, kept alive only by a coverage test that called
it directly. Everything else in those 5,565 lines runs.

This is the finding that governs the rest of the decision. The premise "the
Hermes runtime is dead code to be garbage-collected" does not survive contact
with the reachability graph. What is actually true is narrower: *the name* is
dead.

Two corroborating signals: `scripts/dead-code-check.sh` (vulture at ≥90%
confidence) is already clean over `src/mac`, and its allowlist is 11 lines, so
there was no accumulated backlog of unreachable code hiding here either.

## Decision

**Treat "hermes" as a legacy spelling to be renamed, not a subsystem to be
deleted.** Specifically:

1. **Delete only what is proved unreachable.** That is
   `update_fleet_hermes_surface` and its test. Done in this change.

2. **The CLI noun becomes `persona-instance`.** `mac admin hermes
   register|context|work-context|runtime-proof` already dispatches to
   `register_persona_instance` / `persona_context` / `persona_work_context` /
   `persona_runtime_proof`. The service layer was never Hermes-named; only the
   noun was. `hermes` is retained as an argparse alias, so fleet scripts and
   existing docs keep working.

   Not plain `persona`: that noun is already taken by the persona *definition*
   (`--soul-ref` + `--memory-scope`). What the Hermes group registers is an
   *instance* of one, bound to a tenant and a home. Collapsing the two would
   have produced an argparse conflict and, worse, conflated two tables.

3. **Wire-format strings are frozen.** `mac.hermes_runtime_proof.v1`,
   `mac.hermes.runtime_context.v1`, the `hermes_startup` request field, the
   `hermes_runtime_proofs` / `hermes_work_contexts` dashboard state keys and the
   `/startup/hermes` route are **compatibility surface**, not naming. Deployed
   nodes, the observability console and `fleet-node-install.sh` all read them by
   these exact spellings. Renaming a schema string is a migration with a
   versioned successor (ADR 0021), not part of a rename sweep. They stay.

4. **Module renames are deferred and must be shimmed when done.** The target
   names are:

   | today | target |
   |---|---|
   | `hermes_runtime.py` | `persona_runtime.py` |
   | `hermes_startup.py` | `gateway_startup.py` |
   | `hermes_config_surface.py` | `gateway_config_surface.py` |
   | `hermes_chat_config.py` | `gateway_chat_config.py` |
   | `hermes_adapter.py` | `agent_api_adapter.py` |

   Not done here. `hermes_adapter` in particular is the `mac-hermes` console
   script in `pyproject.toml` and the import path four test modules use for
   `MacApiClient`, so its rename is an installed-entry-point change, not a
   `git mv`. Each rename must land as `git mv` plus a re-exporting shim at the
   old path, because `deploy/fleet-node-install.sh` imports these modules by
   string from shell heredocs where no refactoring tool will find them.

5. **The home-directory split-brain is a separate decision.** Soul files are
   written under a Hermes home by `hermes_runtime.py` while agents run OpenClaw
   out of `$MAC_HOME/openclaw/workspace`. That is a data-location migration with
   live state on every fleet node; it is not a rename and does not belong in
   one.

## Consequences

- The hub keeps starting. Nothing on the live path was removed.
- `mac admin persona-instance` is the documented spelling; `mac admin hermes`
  keeps working indefinitely as an alias. `docs/reference/cli.md` regenerates to
  the new noun.
- `tests/test_hermes_module_reachability.py` pins the measurement: if a future
  change leaves an unreachable symbol in these five modules, the test names it.
  That converts the central claim of this ADR from a dated note into a gate.
- 5,565 lines are *not* removed, and this ADR is the reason. Anyone re-filing
  "delete the Hermes runtime" should read the table above first.
- The renames in (4) remain open work. They are mechanical but wide — roughly
  2,061 references across 68 source files and 125 test files — and each one is
  independently landable behind a shim.

## Alternatives considered

**Delete the five modules.** Rejected on evidence: it removes the hub's own
startup path. This is the option the task description assumed, and the
reachability run is what refuted it.

**Rename everything including the schema strings.** Rejected. The wire strings
are read by deployed fleet nodes that upgrade independently of the hub; a
same-commit rename on both sides is not available to us. ADR 0021 already
requires versioned migrations for exactly this.

**Leave the names alone.** Rejected. The names are why the delete looked safe.
A future reader with a grep and a deadline reaches the same wrong conclusion,
and the next one may not measure first.
