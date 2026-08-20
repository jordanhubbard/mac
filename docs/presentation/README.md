# Presentations

Point-in-time decks about MAC. Each one is pinned to the commit it describes and is never updated
in place — the code moves faster than any deck, and a slide that silently changes meaning is worse
than a stale one.

## Directory convention

```
docs/presentation/<UTC timestamp>-<short commit>/
```

for example `20260820T011224Z-8b424c20/`, where:

- **`<UTC timestamp>`** is `date -u +%Y%m%dT%H%M%SZ` at the time of capture. It sorts
  lexicographically, so `ls` is chronological.
- **`<short commit>`** is `git rev-parse --short=8 HEAD` for the tree that was audited.

Neither part alone is sufficient. The timestamp orders decks and disambiguates two decks built from
the same commit for different audiences; the commit is what makes a claim checkable a year later.
Together they cannot collide.

## What a deck directory should contain

| File | Purpose |
|---|---|
| `README.md` | What the deck covers, its slide list, where it is published, and how to rebuild it |
| `AUDIT.md` | Every factual claim traced to a file, commit or generated reference |
| `build_deck.py` | Deterministic builder for the `.pptx` |
| `images/*.svg` | Diagram sources, hand-authored |

`AUDIT.md` is the part that matters. A capabilities deck ages badly precisely because nobody can
tell which slides are still true; a per-claim source trace makes that answerable by inspection
rather than by memory.

## Text in git, binaries in Google Slides

Only text is committed. Rendered PNGs and the built `.pptx` are generated outputs and are
gitignored; the deck itself is published to Google Slides and linked from its `README.md`.

Two reasons, and the second is not obvious. Megabytes of opaque binary under `docs/` cannot be
reviewed in a diff, so they cost repository weight and return nothing. And
`tests/test_docs_no_operator_identity.py` greps every tracked file under `docs/` for fleet-identity
tokens — with sixteen tokens matched case-insensitively, compressed image data hits one by
coincidence sooner or later. The first PNG tried here matched one of them inside its pixel data at
offset 10020. Keeping `docs/` text-only means that gate keeps reading prose, which is the only
place identity can actually leak.

Which token it was is deliberately not written here: naming it would put it in a checked-in doc and
trip the gate this paragraph describes. Only the test file itself is exempt from the scan. If you
need to know, the failure message names it when it fires.

## Existing decks

| Directory | Commit | Deck | Subject |
|---|---|---|---|
| [`20260820T011224Z-8b424c20`](20260820T011224Z-8b424c20/README.md) | `8b424c20` | [Google Slides](https://docs.google.com/presentation/d/1DXgpB-3fy4IDLynGloaAP349BWrwoVSw8T46VyPdT3M/edit) | What the control plane can do today — object model, task lifecycle, coordination, fleet, measurement |

## Build conventions

- **Audit, do not recall.** Read the tree and the generated references (`docs/reference/cli.md`,
  `docs/reference/openapi.md`) rather than describing MAC from memory. Where the README and the code
  disagree, follow the code and record the discrepancy.
- **Date every measurement.** Ledger figures are true for a window, not forever.
- **State what is proposed.** An ADR marked *Proposed* has not shipped, and a deck that blurs that
  distinction will be caught by the first engineer who reads the code.
- **Keep it out of the published site.** `mkdocs.yml` excludes `presentation/` from the built
  handbook, and `scripts/generate-docs-reference.py` skips it when building the documentation
  inventory, so adding a deck never churns generated files or bloats the site with binaries.
