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
| [`20260904T212515Z-c7a3fee1`](20260904T212515Z-c7a3fee1/README.md) | `c7a3fee1` | [Google Slides](https://docs.google.com/presentation/d/11mrPpsYR-wzRTLYsCiKF3wWcniGP811D6s0zYgPIoV4/edit?usp=drivesdk) | v1.3.5 — OpenShell/OpenClaw onboarding root-cause fixes and fleet dispatch/attestation reliability fixes |
| [`20260902T131314Z-a168e9d0`](20260902T131314Z-a168e9d0/README.md) | `a168e9d0` | [Google Slides](https://docs.google.com/presentation/d/16ZYljibDJ1toiyuBpKmxaiqSjsZ7j69bPDIuGB2tDH4/edit?usp=drivesdk) | v1.3.5 release candidate — artifact publication, deploy resilience, fleet visibility, the contract-test allowance, and the transactional release workflow |
| [`20260831T143751Z-e78a7ba7`](20260831T143751Z-e78a7ba7/README.md) | `e78a7ba7` | [Google Slides](https://docs.google.com/presentation/d/1uPIlC_TYrp3XHd4ARIbrdxNjPiYgAE7pjdi2n_2FUD8/edit) | v1.3.4 — resilient contract gates, supported PostgreSQL CI, host-Python upgrade recovery, and bounded lease-telemetry clock skew |
| [`20260828T104510Z-d8d491d6`](20260828T104510Z-d8d491d6/README.md) | `d8d491d6` | [Google Slides](https://docs.google.com/presentation/d/1yOOzFqRVwhY6opljcPEzfkzQmdjwylsxi1_hFO_8wJ0/edit) | What the control plane can do today — object model, twelve task states, coordination, fleet, measurement at v1.3.0 |
| [`20260825T000816Z-e8040fec`](20260825T000816Z-e8040fec/README.md) | `e8040fec` | [Google Slides](https://docs.google.com/presentation/d/1cLzjGERKojHg0w1FOyUnlqlsVu_OZVM5b_kGc7T3fSw/edit) | What the control plane can do today — object model, twelve task states, coordination, fleet, measurement at v1.2.0 |
| [`20260820T182340Z-bac50778`](20260820T182340Z-bac50778/README.md) | `bac50778` | [Google Slides](https://docs.google.com/presentation/d/1vzkNL3_IM-ophzQWUpJl3JE5L-X3MnEva8m6edeEOQk/edit) | How the control plane is put together — hub↔workers, the life of a task, inside the hub, with live console captures |
| [`20260820T011224Z-8b424c20`](20260820T011224Z-8b424c20/README.md) | `8b424c20` | [Google Slides](https://docs.google.com/presentation/d/1DXgpB-3fy4IDLynGloaAP349BWrwoVSw8T46VyPdT3M/edit) | What the control plane can do today — object model, task lifecycle, coordination, fleet, measurement |

Newest first. The `d8d491d6` deck and the `e8040fec` deck disagree about route count,
ADR 0023/0033 status, and the ledger census, which is the convention working as intended:
each is true of its own commit.

## Screenshots of a live fleet

A deck may include console captures. Two rules, both learned the hard way:

- **Crop or skip the `agents` view.** Its roster lists real agent names, several of which
  `tests/test_docs_no_operator_identity.py` forbids in checked-in docs — and a deck is a shareable
  artifact even when it is not committed. Repository names are fine; that test explicitly permits
  the repo-org slug.
- **Captures are evidence with a date, not a reproducible build step.** They cannot be regenerated
  from the repository, so the deck states when they were taken and the diagrams carry the load that
  has to survive.

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
