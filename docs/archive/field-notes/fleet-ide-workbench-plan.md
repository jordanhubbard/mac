!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Fleet Workbench — Clean-Slate IDE Plan

**Status:** Active  
**Updated:** 2026-07-01  
**Implementation:** `ide/`

## Why this plan exists

The original, detailed Fleet IDE cut-over matrix was committed in
`73bcda3d731b885748c75236adf69a6eacf2210d` and later compressed when ADR 0010
was accepted. That original matrix assessed 57 operator workflows and found
the first IDE increment covered roughly 28 percent of them. This document
restores that backlog as an active product plan without reopening the decision
that `ide/` is canonical.

There are no compatibility requirements for the first-increment UI. The
workbench may change layout, navigation, local state, and component structure
whenever that produces a clearer operator IDE. API and security contracts still
remain authoritative.

## Product model

Fleet Workbench is an operator IDE, not a dashboard with IDE colors.

1. **Cockpit first.** The default surface answers what is running, blocked,
   awaiting review, and unhealthy before it presents controls.
2. **Work is a graph.** Ledger dependencies, workflow nodes, owners, review,
   and publication are represented as one live system rather than unrelated
   tables.
3. **Context stays adjacent.** Selecting work exposes its agent, A2A context,
   activity, evidence, history, and problems without navigation churn.
4. **A2A is a first-class boundary.** Agent Card discovery, declared skills,
   task delegation, task state, messages, and artifacts belong in the normal
   workbench flow.
5. **Progressive detail.** Cockpit, collection, and inspector surfaces share
   one hierarchy: glance, scan, inspect, act.

## Workbench information architecture

| Surface | Primary purpose | Current increment |
|---|---|---|
| Cockpit | Fleet health and live dependency graph | Implemented |
| Work | Searchable, sortable ledger collection | Implemented |
| Workflows | Natural-language plan, graph preview, accept, run inventory | Implemented; run controls remain |
| Agents | Capability and workload mesh | Implemented |
| Runtime | Deltas, runs, and rollouts | Read surface implemented |
| Observability | Control-plane events, notifications, findings | Implemented |
| Connections | A2A Agent Card, service links, redacted secrets | Implemented |
| Context inspector | Agent/A2A details, task thread, rich dispatch | Implemented |
| Bottom panel | Events, terminal sessions, evidence, problems | Implemented; interactive terminal remains |

The recovered parity matrix remains the acceptance backlog:

- Fleet, project, task, agent, and topology navigation
- Rich task dispatch and direct assignment
- Evidence, review, and history actions
- Workflow authoring, graph inspection, runs, cancellation, and retry
- Runtime environments, deltas, runs, and rollouts
- Command, event, policy, notification, metric, and finding observability
- Redacted secret inventory, scope, creation, lifecycle, and audit
- Service links, bridge items, evals, and artifacts
- Multi-target authentication and reconnection

## Reuse strategy

The web bundle should reuse maintained public components where they reduce
Mac-specific surface area:

- [VS Code Codicons](https://github.com/microsoft/vscode-codicons) for familiar
  workbench iconography.
- [React Flow](https://github.com/xyflow/xyflow) for the task and workflow DAG.
- The existing Allotment split-pane implementation for resizable workbench
  regions.

The next architectural decision is whether to package Mac surfaces as VS Code
web extensions on one of these substrates:

- [OpenVSCode Server](https://github.com/gitpod-io/openvscode-server) for a
  close-to-upstream browser workbench and standard extension installation.
- [Eclipse Theia](https://github.com/eclipse-theia/theia) for an IDE framework
  designed to be composed and branded while supporting VS Code extensions.

That decision must be explicit. Either substrate changes the current static
artifact into a server-backed workbench with filesystem, terminal, extension
host, workspace trust, and additional authentication authority. It should not
arrive as a visual refactor.

## A2A interoperability

Mac currently publishes one hub-level Agent Card and maps A2A tasks into its
durable task ledger. Fleet agents are therefore **A2A-routable through Mac**;
the UI must not imply that every worker independently publishes an Agent Card.

The current server advertises protocol version `0.3.0`. The public A2A
specification has advanced to `1.0.0`, including expanded binding and streaming
semantics. Until the server is upgraded, the workbench must read the published
card and use the methods it declares rather than assume current-sdk behavior.
The protocol upgrade should include:

1. Agent Card and operation-name migration.
2. Streaming task status and artifact updates.
3. Remote peer registry and discovery.
4. Capability-aware routing and protocol fallback.
5. Multi-turn task contexts, input-required, and auth-required states.

## Delivery phases

### Phase 1 — Workbench foundation (implemented)

- Clean-slate cockpit layout and navigation
- Aggregate `/dashboard/state` model with streamed invalidation
- Task DAG, filters, deep links, agent inspector, and task thread
- Rich ledger dispatch, selected-agent claim, review request
- A2A Agent Card inspection and `message/send`
- Workflow, runtime, observability, connection, and bottom-panel surfaces
- Responsive compact pane composition

### Phase 2 — Complete operator actions

- Review approve/reject with evidence selection
- Workflow run cancel/retry and definition inspector
- Runtime delta validate/reject/promote and rollout controls
- Secret create/enable/disable and audit inspector
- Service-link navigation, eval, artifact, and bridge inspectors
- Interactive agent terminal and streamed output

### Phase 3 — Extension substrate

- Decide OpenVSCode Server versus Theia
- Define trusted workspace and remote filesystem authority
- Package Fleet Workbench as built-in extensions
- Adopt standard editor, source control, terminal, command palette, settings,
  keybindings, and extension lifecycle

### Phase 4 — A2A 1.0 mesh

- Upgrade the server protocol
- Discover remote Agent Cards and surface negotiated capabilities
- Stream task status and artifact events into the inspector and bottom panel
- Allow direct remote delegation with durable Mac provenance

## Verification criteria

- `npm run build` and `npm audit` pass.
- Browser smoke covers every top-level view, task selection, agent selection,
  A2A delegation, desktop layout, and compact layout without console errors or
  horizontal overflow.
- Python contract tests cover every hub endpoint used by `ide/src/api/mac.ts`.
- The repository-wide contract gate and CodeGraph affected audit pass before
  push.
