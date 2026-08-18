# mac/observe — the hub observability console

A read-only live view of the fleet: tasks and agents **moving through states**,
not a static table of what exists. Served by the hub at **`/ui/console`**.

```
observe/            React + TypeScript + Vite source (this directory)
src/mac/ui/console/ the built bundle, committed, served via the existing
                    /ui/assets StaticFiles mount
src/mac/observability_console.py   the snapshot the console reads
```

## Views

`Live` (movement now) · `Stuck work` (dwell, not counts) · `Agents` (belief vs
evidence) · `Projects` · `Pipelines` · `Dream & nap` · `Telemetry` — plus a
per-task drill-down reached by clicking any task, at `?view=task&task=<id>`.

### The drill-down is partial, and looks it

Clicking a task shows its state history, its agent transcript, the harness
commands audited against it, and its evidence/reviews/publications. Three gaps
in that data are load-bearing, so the UI states each one rather than letting a
blank panel imply "the agent did nothing":

* **Coverage.** Only a small fraction of tasks have any transcript (a few
  percent on the live fleet). The fraction is shown at fleet level on
  *Telemetry* and repeated on any task that has none, because "not recorded" and
  "nothing happened" are indistinguishable from the console and only the first
  is likely.
* **Attribution.** `coding_agent` and `model` were empty on every transcript
  row written before the executor fix, and those rows stay that way forever.
  The console normalises empty to absent and renders **unattributed** — never a
  blank cell that reads as "no CLI ran".
* **Command scope.** `command_audit` records what the MAC harness itself
  spawned, not what the coding CLI ran inside its sandbox. The panel says so,
  every time, including when the list is empty.

A task with *no* transcript, a turn with an *empty payload*, and a transcript
section the hub *could not read* are three different renderings. `tests/
drilldown.test.tsx` asserts all three stay distinguishable.

## The three rules it is built on

**1. Read-only.** `src/lib/http.ts` refuses any method but `GET`/`HEAD` before a
request is made, and it is the only module allowed to touch the network.
`tests/readonly.test.ts` asserts both — the runtime guard *and* a source scan
proving nothing bypasses it. There are no action buttons, no terminal, no
secrets editor. The console can never be the thing that breaks the fleet.

**2. Live.** `/dashboard/stream` is the spine: it emits the hub's observability
cursor, and an `updated` event means "something moved, come and look". The
console then re-reads `/dashboard/observe`. A floor poll runs underneath the
stream so a missed event cannot silently freeze the numbers, and if the stream
fails the console says so in the UI and falls back to polling.

**3. It never renders a plausible zero.** Every section of the snapshot is
assembled independently on the hub; a section that could not be read is
*omitted* and named in `degraded`, so the console can say "unavailable" instead
of "0". Every formatter has an explicit unknown (`—`) return, and the tests in
`tests/honesty.test.tsx` assert that a missing value renders as `unknown` while
a real zero renders as `0`. This codebase has been bitten repeatedly by things
that looked healthy and were not; a dashboard reporting "0 failures" because it
could not reach the hub would be the worst possible addition to it.

## Develop

```bash
npm install
MAC_API_URL=http://<hub>:8789 MAC_UI_PROXY_TOKEN=<token> npm run dev   # :5274
npm run typecheck
npm test
npm run build          # writes src/mac/ui/console/ — commit the result
```

Against a throwaway hub seeded with fleet-shaped data (lots of blocked work, an
agent whose reported status is not believable):

```bash
eval "$(scripts/start-test-postgres.sh)"
uv run --extra dev python observe/scripts/demo_hub.py   # then open /ui/console
```

CI (`observe` job) typechecks, tests, and rebuilds the bundle, failing if the
committed output has drifted from the source.

## Why a new endpoint instead of `/dashboard/state`

`/dashboard/state` times out. Its cost is inherent to what it returns: on the
non-`ide` view it rebuilds the Hermes startup report on **every request** (four
blocking outbound health probes at a 2s timeout each), fans out per agent, per
persona instance and per open task, and reads a dozen tables with no `LIMIT`.
Making it fast means removing keys, and its shape is a contract for the legacy
dashboard, the Fleet IDE, the Electron shell and their tests.

`/dashboard/observe` is additive, has no other consumers, and is built from
server-side `GROUP BY` aggregates. Measured on a seeded hub with 1,705 tasks:
**~30 ms**, against 2.1 s for `/dashboard/state` on the same data.

## Charts

Hand-rolled SVG rather than a charting library. Two marks are needed (stacked
time columns and horizontal magnitude bars), and rolling them keeps the bundle
at 56 kB gzipped while letting every mark follow the house spec exactly — 2px
surface gaps between stacked segments, 4px rounded data-ends, recessive
gridlines, direct labels — which is more work to enforce *through* a library's
theming than to write.

Colour is not a matter of taste here. In-flight task states are an ordered
pipeline, so they use a single-hue **ordinal** blue ramp; exception and terminal
states use the **reserved status palette**, always paired with a label or icon
so hue never carries meaning alone. Both sets were checked with the palette
validator against the dark surface (`#1a1a19`) and pass every gate. Adding a
colour means re-running it.
