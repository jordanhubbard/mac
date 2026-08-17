# ADR 0006 — Agent Client Protocol (ACP) support

- Status: **Proposed**
- Date: 2026-06-16
- Decision owner: Jordan Hubbard
- Update (2026-08-17): the `session/update` → **AgentBus** half of the streaming
  mapping below (topic `acp.session_update`) was built and then removed after a
  production census of `agentbus_streams` found zero streams on that topic ever.
  `session/update` still streams to `/action-events`, which is the ledger the
  finalizer and observability actually read. If AgentBus mirroring is wanted
  later, add it back with a consumer attached.
- Context: agents in mac are driven through a bespoke, Hermes-specific runtime
  seam. `src/mac/task_executor.py` spawns `hermes_cli chat --query <prompt>
  --yolo` over stdio, waits for the process to exit, then reads
  `mac-evidence.json`. The agent contract is **implicit** (no capability
  handshake), the run is **all-or-nothing** (one prompt in, one exit code out;
  no streaming turns, tool-calls, or progress back to the hub during the run),
  and cross-agent comms (`agentbus_service.py`, `messaging_service.py`) are
  **mac-internal** REST over SQLite, poll-only, bearer-token, hub-mediated. A
  third-party/open-source agent cannot be driven by mac, and a mac agent cannot
  be driven by a standard client, without writing to mac's private API.

## Recommendation (the short version)

Adopt the **Agent Client Protocol (ACP)** — the open JSON-RPC 2.0 standard for
**host/client ↔ agent** communication (agentclientprotocol.com; Zed + Anthropic;
the same role LSP plays for editors) — as mac's agent **runtime seam**, in two
roles:

1. **ACP client** — mac *drives* any ACP-compliant agent (Claude Code, Gemini
   CLI, opencode, custom) as a pluggable executor backend, replacing the
   hardcoded `hermes_cli --yolo` invocation. **Highest-value piece**: makes the
   runtime vendor-neutral and gives mac live streaming turns/tool-calls instead
   of exit-code-only.
2. **ACP agent (server)** — mac exposes its agents over ACP so an external
   client (Zed, another hub) can drive a mac agent through the standard
   interface.

Keep Hermes as the default backend until ACP reaches parity; ACP is additive and
feature-flagged.

### Scope: ACP, not (yet) A2A

"ACP" is overloaded. This ADR means the **Agent Client Protocol** (host↔agent).
It is **not**:

- **A2A (Agent2Agent)** — Google/Linux-Foundation peer **agent↔agent**
  coordination. IBM's REST "Agent *Communication* Protocol" merged into A2A
  under the Linux Foundation in Sept 2025, so that name is effectively absorbed.
- **MCP (Model Context Protocol)** — the orthogonal agent↔**tool** layer mac
  already speaks via `src/mac/_hermes/mcp_serve.py`.

The emerging reference stack is *MCP for tools + ACP for host↔agent + A2A for
agent↔agent*. This ADR commits to the **ACP (host↔agent runtime)** axis first,
because that is the most limited part of mac today. A2A (cross-*fleet*
agent-to-agent federation over `agentbus`/`messaging`) is a deliberate, separate,
later track — captured here only as future work.

## Why the current mechanism is the bottleneck

From a survey of the existing comms:

- **Runtime seam (`task_executor.py`)** — run-to-completion; no streaming turn
  protocol; no tool-call/permission channel back to the hub *during* the run; no
  capability negotiation; implicit Hermes-specific contract (`--yolo`, task JSON
  on disk, evidence JSON on disk).
- **AgentBus (`agentbus_service.py`)** — typed content streams, sequence-ordered
  256 KB JSON chunks, **poll-only** (cursor reads), SQLite-backed, point-to-point
  by mac agent-id. No capability registry; no federation.
- **Messaging (`messaging_service.py`)** — QUEUED→DELIVERED control messages,
  **pull-based** delivery, mac-internal message types.
- **Dispatch (`dispatch.py`)** — HTTP claim/lease/poll; request/response, no
  streaming.
- **Auth** — hub-mediated bearer tokens only; no agent-to-agent / peer auth, no
  capability-scoped tokens.

The enabling good news: mac already has the **primitives** ACP needs —
SSE/ndjson streaming (`/action-events/stream`, `/observability/stream`),
AgentBus chunk streams, a permission flow (`mcp_serve` `permissions_list_open` /
`permissions_respond`), and the OpenShell sandbox gate. ACP is largely a matter
of speaking a standard wire format over infrastructure that already exists.

## Decision — target architecture

A new `src/mac/acp/` package: a JSON-RPC 2.0 core with stdio (local subprocess)
and WebSocket/HTTP (remote) transports, plus an `ACPAgentClient` (mac drives an
external agent) and an `ACPAgentServer` (mac agent exposes ACP).

Mapping ACP onto mac:

| ACP concept | mac mapping |
|---|---|
| `initialize` / capability negotiation | New `GET /.well-known/acp` manifest; advertise mac capabilities (sandbox, decomposition, evidence). Closes the missing handshake. |
| `authenticate` | Existing bearer-token `TokenPrincipal` (`api.py`). |
| `session/new` · `session/load` | Task claim + lease (`dispatch.py`); session id ↔ `task_id`/lease. |
| `session/prompt` | The prompt `task_executor` already builds (contract + recalled lessons). |
| `session/update` (streaming) | Reuse **AgentBus chunk streams** + `/action-events` SSE — stream tool calls, plan steps, message chunks back to the hub during the run. |
| tool-call + `session/request_permission` | Bridge to `mcp_serve` permissions + the **OpenShell sandbox** policy gate (a denial = a sandbox policy decision). |
| content (Markdown / MCP JSON) | Evidence/messages serialize cleanly via `models.json_dumps`. |

## Phased delivery (each phase shippable)

- **Phase 0 — Spike + this ADR (small).** Vendor the schema from
  `agentclientprotocol.com/llms.txt`; pick/generate Python ACP types; pin a
  version. No behavior change.
- **Phase 1 — ACP client / executor backend (the core win).** Add `ACPExecutor`
  alongside `SubprocessExecutor`, behind `MAC_EXECUTOR_BACKEND=acp`. mac drives
  an external ACP agent over stdio; map `session/update` → AgentBus +
  action-events for live progress. Prove it by running Claude Code (itself ACP-
  compatible) against a mac task. Keep the deterministic git-finalizer/evidence
  flow as a post-session step.
- **Phase 2 — ACP agent / server.** Expose a mac agent over ACP (stdio first, WS
  for remote). External client opens a session → maps to a mac task; tool calls
  route through mac's tool surface.
- **Phase 3 — Capability + permission + sandbox bridge.** Full `initialize`
  negotiation; wire `request_permission` to the OpenShell sandbox policy and the
  existing permission flow; advertise mac-specific extensions (decomposition,
  evidence) as capabilities.
- **Phase 4 — A2A track (separate, future).** Cross-fleet agent↔agent: publish
  A2A AgentCards, accept A2A task delegation, map to `agentbus`/`messaging`.

## Consequences / risks

- **Protocol churn & name collision** — ACP is young; pin the vendored schema
  version. Always write *Agent Client Protocol* (IBM's "Agent Communication
  Protocol" → A2A) to avoid confusion.
- **Turn model vs mac's proof model** — ACP is interactive/streaming; mac's
  correctness gate is the post-run git finalizer + evidence. Phase 1 keeps the
  finalizer as a post-session step; the "session ended → evidence ready" seam
  needs care.
- **Remote transport & federation** — stdio covers local subprocess agents;
  remote fleet agents need the WS/HTTP transport and a decision on how it rides
  the hub's auth. Decide per phase.
- **Hermes coexistence** — `ACPExecutor` is additive and flagged; Hermes stays
  the default until parity is proven.

## First concrete step

Phase 0 + a Phase-1 spike: vendor the ACP schema, add `src/mac/acp/jsonrpc.py` +
a minimal `ACPExecutor` behind `MAC_EXECUTOR_BACKEND=acp`, and demonstrate one
mac task executed by an external ACP agent with `session/update` streaming into
`/action-events`. That single demo de-risks the whole effort.

## References

- Agent Client Protocol — https://agentclientprotocol.com/get-started/introduction
- agentclientprotocol/agent-client-protocol — https://github.com/agentclientprotocol/agent-client-protocol
- Zed external agents (ACP) — https://zed.dev/acp
- Agent interoperability 2026 (MCP/A2A/ACP convergence) — https://zylos.ai/research/2026-03-26-agent-interoperability-protocols-mcp-a2a-acp-convergence/
