# ADR 0023 - One skill source, thin plugins per coding harness

- Status: **Proposed**
- Date: 2026-08-20
- Amended: 2026-08-25 — Agent Plugins 1.0 is the portable package; the
  human CLI and the agent plugin share one `mac` executable; every agent
  broadcasts what it is doing; only the hub nudges a stalled session
- Decision owner: MAC fleet owner
- Related: [ADR 0013](0013-authoritative-hub-allocator.md) — the hub is
  the one authority, so it is the one nudger
- Related: [ADR 0016](0016-agent-initiated-review.md) — agents decide
  what a task needs
- Related: [ADR 0020](0020-a-running-task-is-not-editable.md) — a running
  task is not editable
- Related: [ADR 0022](0022-a-gate-returns-a-named-decision-not-a-boolean.md)
  — a gate returns a named decision
- Related: [ADR 0026](0026-first-class-operations-emit-bus-events.md) —
  the hub emits object lifecycle; this ADR obliges *agents* to announce
  the work they are in the middle of
- Related: [ADR 0028](0028-installation-is-a-package-not-a-push.md) —
  humans install `mac` as a package; the plugin must not become a second
  installer

## Context

`skills/` holds five skills — `mac-cli`, `agentbus-context`,
`setup-mac-fleet`, `record-user-directed-work`,
`mac-agent-terminal-timeout` — written as operational guidance for whoever is
driving the fleet.

**Nothing delivers them to a coding harness.** They are files in a repository.
A session reads one if it goes looking, and otherwise does not.

That is not a hypothetical cost. On 2026-08-20 an operator had to ask, by
hand, for each of: triaging held tasks against what had actually landed,
checking whether a task's work was already merged before reopening it, and
looking for an existing PR before starting a task. All three are already
written down. `skills/mac-cli/SKILL.md` says, in the section added that same
morning:

> **Check for an existing PR before working a task.**
> `gh pr list --search "<task_id>"`

The session that wrote that paragraph then reopened a task whose work had
already merged, and an agent immediately began re-implementing a merged
module. Writing guidance down and delivering it are different problems, and
only the first is solved.

### mac already knows the harnesses

`coding_agent.AGENT_PRIORITY` routes work to five of them —
`opencode`, `pi`, `claude`, `codex`, `cursor` — and `coding_agent.py` already
handles their credential and config locations: `~/.claude`, `~/.codex/auth`,
`~/.codex/config`, `~/.cursor`, `~/.cursor/mcp`, `~/.config/opencode`.

So the fleet already knows which harnesses exist and where each keeps its
configuration. What it does not do is put the fleet's own operating rules
inside them.

### Why "just add it to CLAUDE.md" is not the answer

CLAUDE.md is one harness's convention, it is repository-scoped, and it is
already long. The rules that matter here are not repository facts — they are
*fleet* facts: claim before you work, triage against branch head first, a task
id in a PR body is not evidence the work landed. They apply to any harness
pointed at a mac-managed project, and several apply outside a repository
entirely.

### A portable plugin format now exists

As of 2026-08 the vendor-neutral **Agent Plugins 1.0** package is the
closest thing to a universal plugin for coding CLIs: a directory with
`plugin.json`, optional Agent Skills under `skills/*/SKILL.md`, and
optional MCP servers in `mcp.json`. Cursor and Codex are listed as
compatible clients. Claude Code keeps its own marketplace/plugin shape
but already understands skills, MCP, and hooks. OpenCode searches
Claude-compatible skill locations and `.agents/skills`, so a standard
`SKILL.md` is almost free there.

The standard does **not** specify installation, package managers, or
install-time shell. That is intentional. A portable plugin whose
install step is "run this script as the user" is a malware vector with
a schema. The CLI the human types, and the plugin the agent loads, must
therefore be two *views of one executable*, not a plugin that writes
binaries onto the machine.

`mac admin mcp serve` already exists. It is JSON-RPC 2.0 over stdio,
four ledger tools (`mac_task_show`, `mac_task_list`, `mac_task_ready`,
`mac_task_create`), and the same `RemoteDispatch` seam the CLI uses.
The plugin must launch **that** server. A second MCP implementation
inside the plugin is how the ledger and the agent silently disagree
about one query parameter — the class of defect that produced the MCP
server in the first place.

What the plugin still does not do: live in the harnesses, announce
work, or get a stuck session moving.

### CLI sessions are already on the bus, and still go silent

AgentBus is broadcast as well as addressed. The closed vocabulary in
`src/mac/agentbus_broadcast.py` already has `task.claimed`,
`task.progress`, `git.worktree_added`, `git.pushed`, `git.pr_opened`,
`git.merged`, `git.canonical_advanced`. `skills/agentbus-context/SKILL.md`
tells a session to read that feed before it starts work. Registered CLI
sessions — this kind of Cursor or Codex conversation included — are
agents on that bus.

They still fail in two directions that this ADR has to name:

1. **They do not reliably say what they are doing.** ADR 0026 measured
   an operator session that spent a working hour re-running
   `mac task stats` because nothing announced the eleven tasks filed
   and claimed in that hour. A peer cannot avoid colliding with work
   it cannot see.
2. **When they stop because they do not know what to do next, everyone
   who can see the silence is tempted to help.** Review-tick history
   already has the shape of that failure: a non-blocking nudge that
   every replica would send becomes a storm, so the review path capped
   *delivered* nudges and put them on one tick. Stall recovery for
   coding sessions needs the same single sender, or the bus becomes a
   pile-on.

## Decision

### 1. One canonical source, Agent Plugins packaging, per-harness adapters

`skills/` stays the single source of truth. The portable package is
**Agent Plugins 1.0**: `plugin.json`, `skills/` copied from that source,
`mcp.json` launching `mac admin mcp serve` (plugin-relative or
`${PLUGIN_ROOT}`, not a second binary). Each harness gets a thin adapter
that *points at* that package:

| Harness  | Delivery surface |
| --- | --- |
| claude   | same package, plus the small Claude marketplace/plugin shim Claude actually loads |
| codex    | Agent Plugins native |
| cursor   | Agent Plugins native; Cursor-only extras (rules, hooks, variables) only when the portable package cannot express them |
| opencode | the same `SKILL.md` via `.agents/skills` (and the Claude-compatible locations OpenCode already searches) plus MCP |
| pi       | its documented instruction surface |

Adapters render; they do not author. A rule that exists in one harness
and not another is a bug, and a per-harness edit is how skills silently
fork.

Do not maintain three plugins. Claude is a shim around the canonical
package, not a fork of the skills.

### 2. Rules that must be obeyed are not the same as reference

Two kinds of content live in `skills/` today and want different treatment.

*Reference* — how the CLI is shaped, which verbs mean something unexpected —
is read on demand and can stay documentation. MCP tools and the skill
text that teaches when to call them are reference until they are not.

*Obligations* — claim before working, triage against branch head, never
close on a mention, **broadcast what you are doing**, **do not nudge a
silent peer** — must be delivered whether or not the session goes
looking. That is the distinction that failed on 2026-08-20: the
reference was consulted, the obligation was not, because nothing
distinguished them.

Obligations are marked as such in the source and rendered into whatever
mechanism the harness offers for always-on instruction.

### 3. The installer targets the user's world, never this repository

Propagation is the entire point, so the install target is a first-class
choice, not an implicit one:

- **global** — the user's own harness configuration (`~/.claude`,
  `~/.config/opencode`, `~/.codex`, `~/.cursor`), applying to every repository
  they work in;
- **repo-local** — the repository the user is standing in when they install,
  for rules that should travel with one project;
- **and explicitly not this repository.** mac already contains `skills/`;
  rendering them back into their own source is a no-op that would make the
  source and the rendered copy two things that can disagree.

An earlier draft of this ADR said adapters "render into `.claude/` or
`.cursor/`" without saying whose. That reads as the source repository, which
is exactly the case that must not happen and the only one with no value.

The installer asks, or takes an explicit flag. It does not guess: writing into
a working tree the user did not nominate is the behaviour most likely to make
someone uninstall it.

### 4. Universal where possible, honest where not

The harnesses genuinely differ, and pretending otherwise produces a lowest
common denominator that serves none of them. The rule is: one source, one
package, per-harness adapters that may use harness-specific mechanisms —
and a test asserting every harness receives every obligation.

Agent Plugins is deliberately modest (skills + MCP, not every vendor's
hooks, subagents, and rules). Use it for the portable core. Use a
harness-specific mechanism when that is how an *obligation* actually
reaches the model. Do not invent a fourth packaging format to paper
over a vendor gap.

### 5. The plugin is versioned and reports what it delivered

A harness carrying stale rules is worse than one carrying none, because the
operator believes the rules are in force. The rendered artifact records the
source revision, and `mac` can report which harness on which host has which
version.

`mac admin plugin status` is that report. Stale is a named state, not a
guess from mtime.

### 6. Skills come under the same guards as docs

Auditing the five skills on 2026-08-20 found them mechanically clean — every
`src/mac/...` path and every `MAC_*` variable they reference still resolves —
and structurally unguarded:

| skill | last touched | commits to `src/mac` since | test |
| --- | --- | --- | --- |
| `mac-agent-terminal-timeout` | 2026-07-13 | 468 | yes |
| `setup-mac-fleet` | 2026-07-25 | 330 | **none** |
| `record-user-directed-work` | 2026-08-11 | 106 | **none** |
| `agentbus-context` | 2026-08-19 | 5 | yes |
| `mac-cli` | 2026-08-19 | 3 | partial |

`skills/` is also absent from the documentation inventory, so it sits outside
the generated-artifact and drift machinery that keeps `docs/` honest. The only
guard that reaches it is the operator-identity check.

Mechanical checks cannot catch the rot that matters: advice still valid in
syntax and no longer true in fact. Two skills have neither a test nor a reader,
and `setup-mac-fleet` — 330 commits behind — is the one that onboards a new
fleet, so its rot costs the most.

Publishing them makes this urgent rather than untidy. An unread skill that is
wrong is a document nobody follows; a *published* skill that is wrong is an
instruction every harness obeys. So skills enter the documentation inventory
and the drift guards as part of this work, not after it.

### 7. One executable; the plugin does not install the CLI

Humans get `mac` from the ordinary package path (wheel / `uv tool` /
Homebrew once ADR 0028 has an artifact). Agents get the same executable
from PATH, or plugin-relative, launched as:

```console
mac admin mcp serve
```

The plugin's `mcp.json` names that command. It does not vendor a second
`mac` and it does not run a postinstall that writes one.

Two installs, one program:

- **human** — `mac` on PATH, for operators and for any hook the harness
  will exec
- **agent** — Agent Plugins (or the Claude shim) pointing at that same
  binary for MCP and at `skills/` for obligations

The installer owns the ugly per-client wiring. It is idempotent, prefers
a symlink or a pointer at one copy of the plugin over copying it four
times, and can uninstall what it installed.

### 8. The installer verb is `mac admin plugin`, not `mac agent install`

The design conversation used `mytool agent install` because that tool
had no `agent` object. mac already does. `mac agent` is the fleet
worker: register, list, hold, resume, heartbeat. `mac agent install`
would read as "install a fleet agent", which is onboarding, and would
collide with that object for as long as both exist.

So the verbs are:

```console
mac admin plugin install
mac admin plugin status
mac admin plugin uninstall
```

They detect which of Claude, Codex, Cursor, OpenCode (and Pi, when it
still has a surface) are present, and they wire those clients to the
canonical package. They do not install `mac` itself; ADR 0028 is that
job.

### 9. Every agent broadcasts what it is doing

A registered agent — fleet worker or CLI session — announces the work
it has taken and the git it is doing, on the existing broadcast
vocabulary, at the natural checkpoints: claim, worktree, push, PR,
merge, trunk moved, progress that a peer could collide with.

Silence is how the hub will later decide the session is stuck. A
working session that never broadcasts therefore *looks* stuck. Heartbeat
is liveness, not progress: a held or idle session may heartbeat without
claiming it is making headway.

Do not add a firehose "still thinking" event. Broadcasts are already
rate-limited and coalesced because `action_events` reached 10.4 million
rows and wedged the hub. Progress is an announcement, not a cursor blink.

If a session *knows* it is blocked on what to do next, it addresses the
hub. It does not broadcast a plea. A broadcast plea is an invitation
for every listener to answer, which is the storm §10 exists to prevent.

### 10. Only the hub nudges a stalled session

When a registered session has a claimed task, or is otherwise expected
to be working, and it has gone silent on progress — or it has addressed
the hub to say it is stuck — **the hub control plane is the only sender
of a stall nudge.**

The nudge is one addressed AgentBus inbox message to that agent: the
next concrete step (ready work it can claim, the obligation it is
missing, the broadcast it did not read). It is not a fleet-wide
broadcast, and it is not a peer-to-peer "get on with it".

One nudger, on purpose:

- Peer agents that observe the silence may read it, may rebase, may
  avoid the same files. They must not address a stall nudge. The
  existing bus convention already says you do not answer a question
  until addressed by name; stall recovery is stricter: you do not
  *ask* either.
- Replicas of the hub do not each send one. The sender is the
  designated hub steward — the same single-tick role review already
  uses — not "any process that can see the bus".
- Waiting on a gate, a human, CI, or a lease is not a stall. Those
  have their own events. Nudging a session that is legitimately
  blocked is noise that trains everyone to ignore nudges.
- Cap and cooldown, the same shape as review's delivered-nudge cap.
  A wedged session that cannot parse the nudge must not be hammered
  until the cap is a denial-of-service. After the cap, the hub records
  that the session is stuck and stops; it does not keep shouting.

How the session *hears* the nudge is a delivery-channel problem
(harness hooks injecting inbox text at a turn boundary; not tmux,
not a prompt that the model start `agentbus wait`). This ADR names
who is allowed to send. The hook layer is a companion decision; it
does not change the single-sender rule.

## Consequences

- Guidance stops depending on a session choosing to read it.
- A new rule is written once and reaches every harness as one plugin
  plus thin shims, not five authored copies.
- Five more delivery surfaces to keep working, each able to break
  independently. The versioned-and-reported requirement exists because that
  breakage is otherwise silent.
- Rendering into a repository's `.claude/` or `.cursor/` means the fleet writes
  files into working trees. That must not collide with a human's own
  configuration, and the boundary needs stating before this is built.
- Coding agents talk to the ledger through the MCP server the CLI
  already has, so the plugin cannot drift from `mac task`.
- The bus becomes the place a session looks to see who is in the same
  tree, instead of polling `mac task stats`.
- Stall recovery has a single sender. The cost is that a dead hub
  cannot nudge; the alternative is a storm, which we have already
  paid for on the review path.

## Alternatives considered

**Keep skills as documentation and rely on operators to read them.** This is
the status quo, and today is the evidence against it: the guidance existed,
was current, was written that morning by the same session that then violated
it.

**Author separately per harness.** Rejected: five copies of a rule is five
places to update and four places to forget. The skills would drift within
weeks, and the drift would be invisible until an agent behaved differently on
one harness.

**One universal format only, no harness-specific mechanisms.** Rejected as
the wrong kind of purity. If a harness offers always-on instruction, an
obligation belongs there; refusing to use it to keep the adapters symmetric
would discard the mechanism that makes obligations work.

**Let the plugin's install step install `mac`.** Rejected by the Agent
Plugins spec (it does not standardize install-time scripting) and by
ADR 0028 (installation is a verified package, not a script that writes
binaries). The human CLI and the agent plugin share one executable
installed the ordinary way.

**Name the installer `mac agent install`.** Rejected: `mac agent` is
already the fleet-worker object. The ChatGPT outline used that argv
because its example tool had no such object. mac does.

**Peer agents nudge a silent peer.** Rejected: that is a nudge storm
with extra steps. The review path already had to cap delivered nudges
and put them on one tick for the same reason. Observation stays
broadcast; recovery stays a single addressed send from the hub.

**Add a high-frequency `session.thinking` broadcast so the hub never
mistakes work for stall.** Rejected: volume is how the bus dies
(`action_events` 10.4M rows). Progress at checkpoints is enough;
heartbeat covers liveness; a session that knows it is stuck addresses
the hub rather than blinking at everyone.
