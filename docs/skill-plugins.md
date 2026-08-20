# Publishing skills to a coding harness

`skills/` is the single source of the fleet's operating guidance. This page is
the operator contract for getting it into a coding harness: what gets written,
where, how it coexists with configuration you already have, and how to remove
it. It implements [ADR 0023](adr/0023-one-skill-source-many-harness-plugins.md).

## Two kinds of content, two delivery surfaces

A rule in `skills/` is one of two things, and the source says which.

**Reference** is read on demand — how the CLI is shaped, which verbs mean
something unexpected. It is delivered as a skill document the harness can load
when it decides the topic is relevant.

**Obligations** must arrive whether or not the session goes looking. They are
marked in the source, on their own line:

```markdown
**OBLIGATION `claim-before-working`** — Register the session and claim the task
with `mac task claim <task_id> <agent_id>` before starting work.
```

Everything else in a skill is reference. Obligations are additionally rendered
into whatever always-on instruction surface the harness offers, because the
failure this exists to fix is precisely an obligation that was written down,
was current, and was not delivered.

Ids are fleet-wide: two skills cannot claim the same one, and `mac admin skills
list` shows which skill owns which.

## Where each harness reads

The harness list is `coding_agent.AGENT_PRIORITY` — the same one that routes
real work — not a second list maintained here.

| Harness | Always-on (obligations) | On demand (reference) | Manifest |
| --- | --- | --- | --- |
| `claude` | `CLAUDE.md` block | `.claude/skills/mac-<skill>/SKILL.md` | `.claude/plugins/mac-skills/plugin.json` |
| `codex` | `AGENTS.md` block | `.codex/skills/mac-<skill>/SKILL.md` | `.codex/skills/mac-skills.json` |
| `cursor` | `.cursor/rules/mac-fleet-obligations.mdc` (`alwaysApply: true`) | `.cursor/rules/mac-skill-<skill>.mdc` | `.cursor/mac-skills/manifest.json` |
| `opencode` | `AGENTS.md` block | `.opencode/skills/mac-<skill>/SKILL.md` | `.opencode/mac-skills.json` |
| `pi` | `AGENTS.md` block | `.pi/skills/mac-<skill>/SKILL.md` | `.pi/mac-skills.json` |

For a **global** install the same layout sits inside the harness's own home
directory — `~/.claude`, `~/.codex`, `~/.cursor`, `~/.config/opencode`,
`~/.pi/agent` — which are the directories `coding_agent` already probes for
credentials.

## The install target is a decision, never an inference

```console
$ mac admin skills install --global            # this user, every repository
$ mac admin skills install --repo /path/to/project   # one project you nominate
$ mac admin skills render --global             # what it WOULD write, writing nothing
```

One of `--global` or `--repo` is required. mac does not install into "the tree
you happen to be standing in": writing files into a working tree nobody
nominated is the behaviour most likely to get the plugin removed.

**This repository is refused outright.** `mac` already contains `skills/`;
rendering them back into their own source produces two copies that can
disagree, and adds nothing. Propagation outside this repository is the entire
point.

## How this coexists with configuration you already have

Three rules, and they are enforced by code rather than by convention.

1. **A file mac shares with you is edited only inside a delimited block.**
   `CLAUDE.md` and `AGENTS.md` belong to their owner. mac writes between
   `<!-- BEGIN mac skill plugin ... -->` and `<!-- END mac skill plugin -->`,
   and everything outside those markers is preserved byte for byte — on
   install, on re-install, and on uninstall. Re-installing replaces the block;
   it never appends a second one.

2. **Everything else lives in mac's own namespace.** Skill documents are
   written as `mac-<skill>`, so a skill of yours with the same name is never
   in the way.

3. **mac refuses to overwrite a file it did not write.** Every file mac owns
   outright carries a provenance line naming the source revision. If a file
   already exists at one of those paths without that line, the install stops
   and names the file rather than taking it. `--force` takes ownership, and
   says so.

Uninstall removes exactly what the receipt recorded — mac's block out of shared
files, mac's own files if they still carry the provenance line, and any
directory that is left empty:

```console
$ mac admin skills uninstall --global
$ mac admin skills uninstall --repo /path/to/project
```

## Versions, and knowing when a harness is stale

A harness carrying stale rules is worse than one carrying none, because the
operator believes the rules are in force. So every artifact names its source
revision — the git commit plus a digest of the skill content, so a dirty tree
is distinguishable from the commit it sits on — and every install writes a
receipt under `~/.mac/skill-plugins/installs.json` recording the host, the
harness, the target, the version, and the exact paths written.

```console
$ mac admin skills status              # per-host, per-harness version and staleness
$ mac admin skills status --host node-a
```

`status` compares each receipt against the current source and reports `stale`
for any that no longer match, plus the harnesses with no install at all. The
receipt file is per-host: `status` reports what *this* host installed, and the
`host` field is what lets receipts collected from several hosts be read
together.

## Nothing publishes without a test

`render` and `install` refuse a skill that no test under `tests/` names, and
refuse a source tree with no `tests/` beside it. An unread skill that is wrong
is a document nobody follows; a published skill that is wrong is an instruction
every harness obeys. `mac admin skills list` shows the `tested` flag per skill.

`--allow-unverified` exists for the case where the caller knows the source was
vetted elsewhere. It is not the default and it is recorded in what it prints.
