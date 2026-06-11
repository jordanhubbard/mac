# Keeping the public mirror (NVIDIA-dev/mac) in sync

`github.com/NVIDIA-dev/mac` is a **de-personalized public mirror** of this
private repo. It is **not** a fork and shares **no git history** — it carries its
own self-contained history of snapshot commits, so nothing about the private
fleet (host/agent names, IPs, handles, tickets) leaks through commit metadata.

## TL;DR

```bash
# preview a sync without pushing (leaves the staged snapshot for inspection)
scripts/sync-public-mirror.sh --no-push

# preview + run the full contract suite on the scrubbed tree
scripts/sync-public-mirror.sh --no-push --run-tests

# do it for real
scripts/sync-public-mirror.sh --run-tests
```

## What the sync does

`scripts/sync-public-mirror.sh` (HEAD by default; pass `--source-sha <sha>` to
pin a revision):

1. **Export tracked tree** — `git archive` of the chosen SHA into a temp dir, so
   only committed files are included (no `.venv`, caches, or untracked cruft).
2. **Strip personal data files** — removes `.tickets/`, `.claude/`, `.codex/`,
   `deploy/*.fleet.yaml`, and the two sync tools themselves
   (`scripts/depersonalize.py`, `scripts/sync-public-mirror.sh`, this doc).
3. **Scrub text** — `scripts/depersonalize.py scrub` rewrites every personal
   token to a generic placeholder (table below).
4. **Fail-closed gate** — `scripts/depersonalize.py check` greps the scrubbed
   tree for any surviving personal token. **If one is found the sync aborts and
   nothing is pushed.** This is the safety net: a name the mapping doesn't yet
   cover blocks the publish instead of leaking.
5. **(optional) test gate** — with `--run-tests`, builds a `uv` venv and runs the
   hermetic contract suite (`-m "not postgres"`) on the scrubbed tree.
6. **Publish** — clones the mirror, replaces its tree with the snapshot, commits
   `Sync de-personalized snapshot (source <short-sha>)`, and pushes `main`. If the
   scrubbed tree is identical to what's already published, it's a no-op.

## The placeholder mapping

The single source of truth is `scripts/depersonalize.py`. Current mapping:

| original | placeholder | notes |
| --- | --- | --- |
| rocky / madmax / natasha / bullwinkle / sparky / puck | hosta / hostb / hostc / hostd / hoste / hostf | agent+host names; valid identifier & hostname |
| do-host1 | node1 | |
| jordanh / jkh / horde | devuser / dev / dev | usernames / bastion user |
| Jordan Hubbard | Dev User | maintainer full name |
| jordanhubbard, jordanhubbard.net | devuser, example.com | GitHub username / personal domain |
| horde-gke.nvidia.com | example.com | bastion host |
| omgjkh / offtera | teamone / teamtwo | Slack workspace slugs |
| rockyandfriends | teamchannel | Slack channel |
| 100.125.137.89 / 100.87.229.125 / 146.190.134.110 | 100.64.1.1 / 100.64.1.2 / 203.0.113.10 | tailnet + public IPs |

Each name is matched in lower / Title / UPPER spellings. Agent names are replaced
unbounded (catches `\n`-glued string literals); the rest only on token boundaries
(so e.g. `horde` never chews `chOrder` in a vendored XSD). The deliberately-kept
generic items — bare `Jordan`/`jordan` sample fixtures, the fictional `Jordan
Park` persona, opaque Slack IDs, and the word `nvidia` (load-bearing in
`nvidia.com/gpu`, `NVIDIA_API_KEY`, `nvcr.io/nvidia`) — are **not** scrubbed.

## Extending the mapping

When the gate aborts on a new token (or you add a new fleet host), edit
`AGENT_NAMES` / `BOUNDED_NAMES` / `LITERALS` in `scripts/depersonalize.py`, then
re-run with `--no-push --run-tests` until the check is clean and tests pass.

## Why no shared history

The mirror is updated as fresh snapshots on its own `main`, not by pushing this
repo's branches. Consequences:

- The two repos **cannot** be fast-forward-merged into each other; changes flow
  one way (private → public) only, through this script.
- The mirror's history is a clean changelog of de-personalized snapshots, each
  annotated with the source short-SHA for traceability.
- `uv.lock` **is** carried over (it's source, not an artifact) so the public repo
  builds reproducibly.
