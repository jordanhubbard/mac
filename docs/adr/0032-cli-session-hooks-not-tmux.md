# ADR 0032: CLI sessions use each harness's hooks, not tmux, for recording and AgentBus injection

- Status: Proposed
- Date: 2026-08-24
- Decision owner: MAC fleet owner
- Related: [ADR 0023](0023-one-skill-source-many-harness-plugins.md) — one
  MAC source, thin per-harness adapters; that ADR delivers *skills*, this one
  delivers *live I/O*
- Related: [ADR 0026](0026-first-class-operations-emit-bus-events.md) — the
  hub emits; a CLI session that never consumes is still deaf
- Related: [ADR 0029](0029-the-route-search-path-is-a-fleet-contract.md) — the
  five coding routes this applies to
- Related: [ADR 0007](0007-hermes-boundary-mood-nap-soul-memory.md) — the
  human-facing runtime is not the coding-CLI session supervisor

## Context

The operator asked for this on 2026-08-24: optimize the CLI sessions that
run for each agent so they use the well-known hooks on Claude, Codex
("code"), Cursor, and OpenCode, and **do not** run those CLIs under tmux or
at any other outer-supervisor layer. Every one of those CLIs now has
advanced hooks that can record the session's work and inject messages into
the model's context from AgentBus.

That is one decision with two jobs, and a level that has to be named
because the wrong level is the one people reach for.

### What exists today, verified in the tree

**A task executor launches the coding CLI as a captured subprocess.**
`executor_prompt._run_captured` is the seam. `docs/in-flight-agent-messages.md`
states the consequence in one sentence: once a task starts, nothing can
reach the coding CLI, so a correction arrives after the mistake is finished.

**The shipped "fix" is a convention.** `executor_prompt._coordination_section`
tells the model to start `mac admin agentbus wait $MAC_AGENT_ID` as a
background harness task, restart it with `--after-cursor`, and treat a
message as a correction. `docs/in-flight-agent-messages.md` documents that
shape and cites AgentRadio's `wait_for_mention.sh` as the provenance.

That is the wrong level for two independent reasons:

1. **It is a prompt the model may follow.** Delivery of AgentBus traffic
   cannot depend on the model choosing to launch a watcher. A session that
   forgets, a harness that has no background slot, and a sandbox that
   forbids detached processes all become deaf.
2. **It already failed for registered CLI sessions.** ADR 0026 recorded
   two `mac.repo.update.result.v1` replies addressed to a registered session
   on 2026-08-21, opened and closed within ~20ms, and never read.
   `agentbus wait` is a blocking inbox read. An interactive CLI has no
   background loop to put it in. `drain` / `pending` were added so a
   *caller* can poll without blocking; they still require a caller.

**There is no tmux in this repository.** A search of `src/`, `deploy/`,
and `docs/` at `e8040fec` finds none. The wrong level is still real: wrap
the coding CLI in tmux, screen, dtach, or a PTY (`script`,
`worker_debug_terminal`'s debug PTY) so an outer process can
`capture-pane` for a transcript and `send-keys` for an inbound message.
That fights the CLI. It is also how credentials leak into scrollback, how
a compacting session loses the injected text, and how a Cursor or Codex
TUI that is not a dumb terminal silently ignores the keystrokes.

**Recording today is stdout of that captured subprocess**, plus whatever
the model writes as MAC evidence. Interactive host sessions have neither.
The session's tool calls, files touched, and stop reason are inside the
CLI; scraping a pane does not parse them.

### The CLIs already have the right surface

The four named harnesses, plus `pi` (the fifth `coding_agent.AGENT_PRIORITY`
route), each document a hook or plugin plane that fires at turn
boundaries. Those are the points where a process MAC owns can run
*inside* the CLI, not around it.

| Harness | Config the CLI already reads | Inject (context the model will see) | Record (work the hub should keep) |
| --- | --- | --- | --- |
| Claude | `~/.claude/settings.json` `hooks` (user) and `.claude/settings.json` (project) | `UserPromptSubmit` / `SessionStart` JSON `hookSpecificOutput.additionalContext`; `Stop` / `PreCompact` for the next turn | `PreToolUse`, `PostToolUse`, `Stop`, `SessionEnd` |
| Codex ("code") | `~/.codex/hooks.json` or inline `[hooks]` in `~/.codex/config.toml`; `features.hooks` | `UserPromptSubmit`, `SessionStart` (including compact-resume); same event names as Claude | `PreToolUse`, `PostToolUse`, `Stop`, `SessionEnd` |
| Cursor | `~/.cursor/hooks.json` (user) and `.cursor/hooks.json` (project) | `sessionStart` `additional_context`; `postToolUse` `additional_context`. `beforeSubmitPrompt` is a gate (`continue`), not a rewrite — do not pretend it injects | `afterFileEdit`, `postToolUse`, `stop`, `afterAgentResponse` |
| OpenCode | plugin in `opencode.json` / the documented plugin array | `chat.message` mutates `output.parts` before the LLM call; `experimental.session.compacting` survives compact | `tool.execute.before` / `tool.execute.after`; `event` for `session.*` |
| Pi | its documented hook / instruction surface, when present | the same two jobs, on whatever it documents | the same two jobs |

Event names drift. The table is the *kind* of surface, dated 2026-08-24
against each vendor's public hook docs. An adapter that hard-codes a
JSON key the vendor renamed is a bug in the adapter, not a reason to
go back to tmux.

Two honesty notes that decide the Cursor and OpenCode wiring:

- Cursor's `beforeSubmitPrompt` currently accepts `{ "continue": true }`
  and does **not** rewrite the prompt. Injection on Cursor is
  `sessionStart` / `postToolUse` `additional_context`, not send-keys
  and not a prompt prefix the hook cannot apply.
- OpenCode's `chat.message` *can* push parts the model will see. Other
  OpenCode hooks (`session.idle`) cannot. Use the hook that injects;
  do not wrap OpenCode in tmux because a different hook is fire-and-forget.

## Decision

**The coding CLI is the session. MAC installs a hook adapter into that
CLI. Nothing wraps the CLI in order to record it or talk to it.**

### 1. Two jobs, one MAC program, thin per-harness wiring

One program MAC owns (a `mac admin` verb or a small console script the
hooks invoke) does both jobs:

1. **Inject.** Non-blocking `drain` of this agent's AgentBus inbox.
   Return the payloads as additional context in the harness's hook
   output shape. Empty inbox → empty context, not an error. A drain
   failure must not block the user's turn.
2. **Record.** On tool/file/stop/session-end events, emit a secret-free
   structured record to the hub (`mac.cli_session.turn.v1` or the
   existing evidence/action-event path). Credentials, token values, and
   raw pane bytes do not belong in that record.

Each harness gets a thin config snippet that *points at* that program
with the event name the vendor documents. Adapters render; they do not
author. A hook that exists on Claude and not on Codex is a bug, the
same rule ADR 0023 uses for skills.

This is not ADR 0023. Skills are static obligations. This is live I/O
with a running session. Same delivery pattern, different payload.

### 2. The wrong levels are refused, not deferred

These are not "later". They are how this fails:

- **tmux / screen / dtach** as a session supervisor, including
  `send-keys` for inbound AgentBus and `capture-pane` for a transcript.
- **A PTY wrapper** (`script`, a debug PTY, a "headless tmux") whose
  job is to be the recording or injection plane. `worker_debug_terminal`
  stays a debug surface; it is not how fleet sessions hear the bus.
- **A prompt convention** that the model start `agentbus wait` in the
  background. Keep `wait` / `drain` as CLI verbs for operators and for
  harnesses that have a real background slot. Do not make them the
  delivery architecture.
- **The human-facing runtime wrapping the coding CLI** (OpenClaw /
  Hermes gateway, a Slack bot that shells out). ADR 0007's boundary:
  conversation is not the coding session. The coding CLI's own hooks
  are.
- **An MCP tool the model must elect to call** in order to hear the
  bus. Pull is optional. Injection at `UserPromptSubmit` /
  `chat.message` / `sessionStart` is not.
- **Git hooks.** They fire on commits, not turns. The work to record
  is the turn.
- **Stdout scrape of `_run_captured` as the only record of an
  interactive session.** Captured stdout remains a legitimate *attempt
  log* for a sandboxed task run. It is not a session protocol.

### 3. Same adapter in the sandbox as on the host

A fleet worker running `claude -p` / `codex exec` / `cursor-agent` /
`opencode run` inside OpenShell is still a CLI session. Install the
same hook config into the sandbox home the coding CLI will read, so a
mid-task AgentBus correction lands at the next turn boundary without
the model starting a watcher.

`_run_captured` stays. It is how the executor bounds the process and
keeps an attempt log. Hooks sit *inside* that process, which is the
level that can inject context the model will actually see.

### 4. Delivery is a channel; obedience is not

`docs/in-flight-agent-messages.md` already says this and it remains
true: injecting a correction does not make the agent obey. The hook
guarantees the text is in context for the next model call. Whether the
agent stops, yields a file, or ignores a peer is still a matter of
instructions (ADR 0023) and review.

### 5. Failure must not take down the session

A hook that cannot reach the hub records nothing and injects nothing
for that turn. It does not block the prompt, deny a tool, or exit
non-zero in a way the CLI treats as fatal. The session is the product;
the bus is the announcement. Same posture as ADR 0026 §2 for emission.

## Consequences

- Follow-up work installs the adapter and the four (five) config
  snippets, with a test per harness that the hook config names a
  binary that exists and that an empty inbox produces a valid empty
  hook payload. This ADR does not ship that installer.
- Once hooks are installed, `_coordination_section` should stop
  telling every executor to background-wait. `docs/in-flight-agent-messages.md`
  remains the inbox API (`wait` / `pending` / `drain`); it ceases to
  be the delivery architecture for CLI sessions.
- Interactive host sessions registered as agents become first-class
  bus participants without a tmux babysitter. That is the defect ADR
  0026 measured and this ADR names the layer for.
- Cursor injection is weaker than Claude/Codex/OpenCode until
  `beforeSubmitPrompt` can add context. The adapter uses the events
  Cursor actually honors, and the gap is recorded rather than papered
  over with tmux.

## Not this ADR

- Changing `AGENT_PRIORITY` or the route ladder (ADR 0029).
- Making AgentBus emission complete (ADR 0026). This is consume, not
  emit.
- Replacing OpenShell isolation, leases, or the merge queue.
- Recording raw transcripts into Qdrant. ADR 0030 still wants an
  extract, not a pane dump.
