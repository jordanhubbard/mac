# Agent Mesh Investigation — Ground-Truth Findings

Read-only ground-truth investigation for parent task_2171121648d0423cb1ae993bdf9708ad.
Scope: reproduce and characterize two reported IDE (`ide/`) defects with exact
`file:line` evidence. No source or test logic was changed by this investigation.

- Baseline commit: `0c61ad3 MAC OpenShell sandbox baseline`
- Contract test under scrutiny: `ide/tests/workbench-project-tree.spec.ts`

## Environment / runnability of the IDE suite

- `npm ci` (in `ide/`): **succeeds** — 57 packages installed from the npm registry (network permits registry access).
- `npm run typecheck` (`tsc --noEmit`): **passes** with no diagnostics.
- `npm run test:ui` (`playwright test`): **cannot run in this environment.** The
  Playwright browser download is blocked by network policy — `npx playwright install chromium`
  returns HTTP 403 `policy_denied` for `cdn.playwright.dev`, and no system Chromium/Chrome
  is present. The two named Playwright tests therefore could not be executed here; their
  pass/fail was established by static trace against the mocked fixture data instead.

## Defect 1 — "Clipped Agent Mesh strip"

Reported concern: under vertical pressure with many agents (12+), avatar buttons get
clipped (avatar box escaping the strip, buttons shrinking below the 44×44 click target,
or the composer overflowing the mesh).

### Reproduction / contract mapping

Test: `ide/tests/workbench-project-tree.spec.ts:223` — "agent selector preserves every
click target under vertical pressure". It renders 12 agents (each with 24 capabilities),
sets the viewport to 1440×600, then asserts:

- first & last strip button `boundingBox().width >= 44` and `height >= 44`
- last avatar `y` within `.mesh-agent-strip` vertical bounds
- `.mesh-composer` bottom within `.agent-mesh` bounds (+1px tolerance)
- `.agent-inspector` scrolls (`scrollHeight > clientHeight`)

Relevant render: `ide/src/components/AgentMesh.tsx:114` (`.mesh-agent-strip` with a
`<button>` per agent, each containing `<span class="agent-avatar">` and a `.presence` dot).

### Per-assertion verdict against current CSS

- Button ≥ 44×44: **satisfied.** `ide/src/styles.css:333` pins
  `width:44px; height:44px; min-width:44px; min-height:44px; flex:0 0 44px`. The strip
  (`ide/src/styles.css:332`) is a horizontal flexbox with `overflow-x:auto`, so 12 buttons
  scroll sideways and never shrink below the 44×44 click target.
- Avatar y within strip: **satisfied.** `.agent-avatar` is 31×31 (`ide/src/styles.css:119`)
  and is centered via `place-items:center` inside the 44px grid button
  (`ide/src/styles.css:333`). The strip uses `overflow-y:hidden` with `flex:none` buttons,
  so the avatar box stays inside the strip's vertical bounds.
- Composer within mesh: **satisfied.** `.agent-mesh` is a full-height flex column
  (`ide/src/styles.css:327`, `height:100%; min-height:0`). Header, tabs, strip and
  `.mesh-composer` (`ide/src/styles.css:358`) are `flex:none`, while `.agent-inspector`
  (`flex:0 1 auto; overflow-y:auto`, `ide/src/styles.css:336`) and `.mesh-thread`
  (`flex:1`) absorb vertical pressure, keeping the composer inside the mesh box.
- Inspector scrolls: **satisfied.** `.agent-inspector` (`ide/src/styles.css:336`) has
  `min-height:0; flex:0 1 auto; overflow-y:auto`; with 24 capabilities in a 600px viewport
  its content overflows so `scrollHeight > clientHeight`.

### Verdict

**Already-correct (not a real defect) on baseline `0c61ad3`.** Every assertion in the
contract test is satisfied by the current CSS; no rule clips the strip, shrinks the click
target, escapes the avatar, or overflows the composer.

Responsible / load-bearing rules to preserve during any future edit:
`ide/src/styles.css:332` (strip: horizontal, `overflow-x:auto`, `overflow-y:hidden`),
`ide/src/styles.css:333` (fixed 44×44 `flex:0 0 44px` buttons), `ide/src/styles.css:336`
(inspector is the scrolling/shrinking region), `ide/src/styles.css:327` (mesh full-height
flex column).

### Remediation recommendation

No code change required for the reported defect. If a regression is later introduced,
the fix must keep the button at a fixed 44×44 with `flex:0 0 44px`, keep the strip's
`overflow-x:auto`, and ensure `.agent-inspector` (not the strip or composer) is the flex
item that shrinks/scrolls under vertical pressure.

## Defect 2 — "Slack advertisement projection"

Reported concern: the projected verified advertisement string (channel ordering/joining,
identity label, verified suffix) may not match the expected slack+telegram projection.

### Reproduction / contract mapping

Test: `ide/tests/workbench-project-tree.spec.ts:216` — "agent inspector exposes the
verified OpenClaw service advertisement". It expects the exact visible string:

```
openclaw · gateway · delegate for MAC Hive · openshell · slack + telegram · verified
```

Projection under test: `chatGatewayLabel` in `ide/src/components/agentFacts.ts:52`,
rendered by `ide/src/components/AgentMesh.tsx:189`
(`<Definition label="OpenClaw" value={chatGatewayLabel(item)} />`).

Fixture (`ide/tests/workbench-project-tree.spec.ts:66`): `openclaw_runtime`
`{ implementation: "openclaw", mode: "gateway", confinement: { provider: "openshell" }, verified: true }`;
`representation` `{ mode: "delegated", identity: "MAC Hive" }`;
`chat_gateway` `{ implementation: "openclaw", public_identity: "MAC Hive",
confinement: { provider: "openshell" }, channels: { slack: { enabled: true },
telegram: { enabled: true } }, verified: true }`.

### Static trace of `chatGatewayLabel` against the fixture

- `implementation` = `"openclaw"` (`agentFacts.ts:57`)
- `mode` = `"gateway"` (`agentFacts.ts:59`)
- `confinement` = `"openshell"` (`agentFacts.ts:60`)
- `activeChannels` = `["slack","telegram"]` (insertion order of enabled channels,
  `agentFacts.ts:62`) → joined with `" + "` → `"slack + telegram"` (`agentFacts.ts:77`)
- `runtimeVerified` = `"verified"` (`agentFacts.ts:65`)
- `identity` = `"MAC Hive"`, `representationMode` = `"delegated"` → `identityLabel` =
  `"delegate for MAC Hive"` (`agentFacts.ts:70`–`agentFacts.ts:73`)
- Final array joined with `" · "` (`agentFacts.ts:77`–`agentFacts.ts:79`):
  `openclaw · gateway · delegate for MAC Hive · openshell · slack + telegram · verified`

### Verdict

**Already-correct (not a real defect) on baseline `0c61ad3`.** The projected string is
byte-for-byte identical to the contract expectation: channel ordering (`slack + telegram`),
`" + "` channel join, `" · "` field join, `delegate for MAC Hive` identity label, and the
trailing `verified` suffix all match.

Composition lines responsible: `ide/src/components/agentFacts.ts:77` (array assembly +
`activeChannels.join(" + ")`), `ide/src/components/agentFacts.ts:79` (`join(" · ")`),
`ide/src/components/agentFacts.ts:70`–`ide/src/components/agentFacts.ts:73` (identity label).

### Remediation recommendation

No code change required. If a regression is later introduced, preserve the ` · ` field
join, the ` + ` channel join, the enabled-channel filtering/order at
`ide/src/components/agentFacts.ts:62`, and the `mode === "gateway"` + non-`direct`
representation path that yields `delegate for <identity>`.

## Overall conclusion

Both reported defects are **already-correct** against their contract-test assertions on
baseline `0c61ad3`; no clipping, click-target shrinkage, avatar escape, composer overflow,
or advertisement-string mismatch exists in the current source. `npm ci` and
`npm run typecheck` pass; `npm run test:ui` could not be executed because Playwright browser
downloads are blocked by network policy, so the two named tests' outcomes were established by
static trace against the mocked fixtures rather than live execution.

## Remediation / finalize decision (parent contract-repair)

Acting on the investigation above, the remediation child made **no source change**:
both reported defects are already-correct against their contract-test assertions, so
forcing an edit would be an unwarranted change. The load-bearing rules were re-verified
directly:

- Defect 1 (clipped Agent Mesh strip): `.mesh-agent-strip button` is pinned to a fixed
  `44x44` click target with `flex:0 0 44px` and `place-items:center`
  (ide/src/styles.css:333); the strip scrolls horizontally (`overflow-x:auto;
  overflow-y:hidden`, ide/src/styles.css:332); `.agent-inspector` is the shrink/scroll
  region (ide/src/styles.css:336) and `.mesh-composer` stays inside the full-height mesh
  column (ide/src/styles.css:327). No clip, shrink, avatar escape, or composer overflow.
- Defect 2 (Slack advertisement projection): `chatGatewayLabel`
  (ide/src/components/agentFacts.ts:52) composes the enabled channels with `" + "` and the
  fields with `" · "`, yielding exactly
  `openclaw · gateway · delegate for MAC Hive · openshell · slack + telegram · verified`,
  byte-for-byte matching the contract expectation in
  ide/tests/workbench-project-tree.spec.ts:216.

Verification performed by the remediation child:

- `ide/`: `npm ci` (57 packages) and `npm run typecheck` (`tsc --noEmit`) both pass with no
  diagnostics.
- `ide/` `npm run test:ui`: the two named Playwright tests could **not** be executed —
  `npx playwright install chromium` is blocked by network policy and no system Chromium is
  present, so both fail only with `browserType.launch: Executable doesn't exist`, i.e. an
  environment limitation, not an assertion failure. Their expected outcomes were confirmed
  by static trace against the mocked fixtures.
- Repository contract gate: `python3 scripts/bootstrap-project.py` and
  `scripts/run-contract-tests.sh` both pass (9216 passed / 4 skipped in the bulk slice and
  4 passed / 39 skipped in the second slice; exit 0).
