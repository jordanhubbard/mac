# ADR 0025 — There is one hub UI, and it is the one the hub serves

- Status: **Accepted**
- Date: 2026-08-20
- Decision owner: MAC fleet owner
- Supersedes: the canonical-surface half of ADR 0010 (Fleet IDE cut-over)
- Related: ADR 0018 (task graph under progressive disclosure), ADR 0024 (the
  dashboard streams the bus) — both assume a single UI and neither named it

## Context

Measured 2026-08-20 against a live hub:

| tree | built by | where it lands | who serves it |
|---|---|---|---|
| `observe/` | `make observe-build` | `src/mac/ui/console/` (3 files, committed) | the hub, at `/ui` |
| `ide/` | `make run-gui` → `ide-run` | a local Vite dev server on `IDE_PORT` | **nobody** |

`api.py` mounts `Path(__file__).with_name("ui")` at `/ui/assets` and every
`/ui` route returns `ui_dir/"console"/index.html`. `ide/` is not mounted, not
packaged into the wheel, and not deployed. A live `curl /ui` returns
`<title>MAC - fleet observability</title>`.

Meanwhile the Makefile called `ide/` **canonical** in six places, `make run-gui`
ran it, and the deploy script's "hub UI access" banner told operators to open it.
So an operator who ran `make run-gui` to look at the hub UI got a *different
application*, found it did not reflect the fleet, and reported the hub UI as
broken. That report was correct about what they saw and wrong about what it
was — the worst kind of bug report to receive, and the tree caused it.

Two UIs is survivable. Two UIs where the label says one thing and the
deployment says another is not: it makes every UI conversation start with a
disambiguation, and it sends bug reports to the wrong tree.

ADR 0010 declared `ide/` canonical on 2026-06-27 and required that "all new UI
work must target `ide/`". The eight weeks since say the fleet did the
opposite: the console got the live view, stuck work, merge queue, telemetry and
task drill-down, was built into the Python package, and became the thing that is
actually running. A declaration that operations has already overruled is not a
decision, it is a stale label.

## Decision

**The hub UI is the observability console built from `observe/`.** It is the
product. It is what `/ui` serves, what ships in the wheel, and what `make
run-gui` runs.

**`ide/` is an unshipped local prototype.** It keeps its own explicit entry
points (`ide-run`, `ide-build`, `ide-preview`, `ide-package`) and is named as a
prototype everywhere it appears. It is not in `install`, `build`, or `package`,
and the word *canonical* does not attach to it.

Concretely:

- `run-gui` → `observe-run`: serves `observe/` — the same source tree whose
  build output the hub serves.
- `install-gui`, `build-gui`, `package-gui` build and package
  `$(OBSERVE_BUNDLE)`, not `ide/dist`.
- The deploy banner points at `http://<hub>/ui` and offers the prototype as an
  explicitly optional step 3.

### Why not the other way

Making the hub serve the Fleet IDE was the alternative, and it is a real
option — it is the mutating surface, and the console cannot ever be that by
design. It was rejected *for now* because it is not a labelling change: it needs
a bundle committed into the package, an asset base under the existing mount, an
auth story for a mutating surface reached without a login shell, and a decision
about whether the read-only guarantee survives being served from the same
origin. None of that is blocked by this ADR. If the fleet does that work, the
honest sequence is: serve it, then rename it — not rename it, then hope.

## Consequences

- `make run-gui` no longer starts the IDE. Operators who want it run
  `make ide-run`; the deploy banner prints that line.
- `make package-gui` now produces `dist/mac-hub-ui.tar.gz`. The IDE tarball
  moved to `make ide-package` and keeps its `dist/mac-ide-web.tar.gz` name.
- `make install` and `make build` no longer require `ide/node_modules`.
- ADR 0010's parity matrix stays as history; its "canonical" claim does not.

## Enforcement

`tests/ui/test_hub_ui_is_one_tree.py` walks the Makefile's own dependency graph
and fails if:

1. `run-gui` reaches `ide-run`/`ide-dev` or any `ide/` recipe;
2. any GUI lifecycle target (`install`, `build`, `install-gui`, `build-gui`,
   `package-gui`, `run-gui`) builds or runs `ide/`;
3. `observe/`'s Vite `build.outDir` stops resolving to the directory `api.py`
   serves, or its `base` stops matching the `/ui/assets` mount;
4. the Makefile calls the Fleet IDE canonical again.

Prose is what lied last time, so the test reads the wiring, not the docs.
