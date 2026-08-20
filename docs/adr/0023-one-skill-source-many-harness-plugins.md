# ADR 0023 - One skill source, thin plugins per coding harness

- Status: **Proposed**
- Date: 2026-08-20
- Decision owner: MAC fleet owner
- Related: ADR 0016 (agents decide what a task needs), ADR 0020 (a running task
  is not editable), ADR 0022 (a gate returns a named decision)

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

## Decision

### 1. One canonical source, per-harness delivery

`skills/` stays the single source of truth. Each harness gets a thin adapter
that renders the same content into whatever that harness reads:

| Harness  | Delivery surface |
| --- | --- |
| claude   | `.claude/skills/` + plugin manifest |
| codex    | `AGENTS.md` fragment + config |
| cursor   | `.cursor/rules` |
| opencode | `~/.config/opencode` |
| pi       | its documented instruction surface |

Adapters render; they do not author. A rule that exists in one harness and not
another is a bug, and a per-harness edit is how skills silently fork.

### 2. Rules that must be obeyed are not the same as reference

Two kinds of content live in `skills/` today and want different treatment.

*Reference* — how the CLI is shaped, which verbs mean something unexpected —
is read on demand and can stay documentation.

*Obligations* — claim before working, triage against branch head, never close
on a mention — must be delivered whether or not the session goes looking. That
is the distinction that failed today: the reference was consulted, the
obligation was not, because nothing distinguished them.

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
rendering pipeline, per-harness adapters that may use harness-specific
mechanisms — and a test asserting every harness receives every obligation.

### 5. The plugin is versioned and reports what it delivered

A harness carrying stale rules is worse than one carrying none, because the
operator believes the rules are in force. The rendered artifact records the
source revision, and `mac` can report which harness on which host has which
version.

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

## Consequences

- Guidance stops depending on a session choosing to read it.
- A new rule is written once and reaches every harness.
- Five more delivery surfaces to keep working, each able to break
  independently. The versioned-and-reported requirement exists because that
  breakage is otherwise silent.
- Rendering into a repository's `.claude/` or `.cursor/` means the fleet writes
  files into working trees. That must not collide with a human's own
  configuration, and the boundary needs stating before this is built.

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
