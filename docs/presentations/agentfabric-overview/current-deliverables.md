# Current deliverables — AgentFabric overview

Status of the two members of this document pair, as of the last recorded
regeneration.

**Built from:** `jordanhubbard/mac` at `2976182`, 2026-08-30.

## Members

| Member | Local artifact | Published location | Publication authorized |
| --- | --- | --- | --- |
| Presentation | `docs/presentations/agentfabric-overview/agentfabric-overview.pptx` — 20 slides, speaker notes on every slide | none | no |
| Narrative | `docs/presentations/agentfabric-overview/agentfabric-overview.docx` — 67 headings, six parts | none | no |

Neither member has been published. There is no Google Slides or Google Docs
resource for this package, and `publish_google_workspace.py` has no default
destination: publishing requires `--slides-id` and `--authorized-by`, supplied by
the repository owner. Do not record a published location here until a
publication receipt exists in `_build/agentfabric-overview/publish-receipt.json`.

## Generated artifacts

Both members and all QA output are build products, reproduced by
`regenerate.sh`, and are gitignored — the authoring package is the durable
artifact. QA output lands in `_build/agentfabric-overview/`:

| Artifact | Contents |
| --- | --- |
| `document-pair-manifest.json` | member paths, publication authorization state, package element map |
| `acceptance.json` | slide count, notes coverage, narrative heading spine, pass/fail |
| `rendered-slides/slide-01.png` … `slide-20.png` | 1280 × 720 renderings used for visual review |
| `contact-sheet.png` | all 20 slides on one sheet |

## Deck structure

Marketing first, then mechanism, then limits.

1. One control plane for a fleet of AI agents
2. Ask once. The fabric carries it to production
3. A chat window is not an operating model
4. Four properties the fabric guarantees
5. One request, end to end
6. Part one — built on the stack, not instead of it
7. Where AgentFabric leverages NVIDIA technology
8. Where AgentFabric leverages open source
9. What AgentFabric adds that nothing above provides
10. The trust boundary, drawn explicitly
11. Part two — actors, roles, and who is allowed to decide
12. The cast, and the one authority each actor holds
13. The life of one task
14. Coordination is a town square, not a switchboard
15. A heterogeneous fleet, modelled honestly
16. Many models, one ordered route, one meter
17. The fleet measures itself
18. Scale of the implementation
19. Stated honestly: implemented, decided, proposed
20. Reuse the stack. Own the truth

## Narrative structure

Six parts, matching the deck's claim order: framing and the problem; what the
system does; built on the stack (NVIDIA re-use, OSS re-use, what AgentFabric
adds, the trust boundary); actors, lifecycle, coordination, fleet, models, and
evidence; scope stated honestly; and how to adopt it.

## Known limitations of this edition

- Counts on slide 18 are surface area measured on the commit above. They must be
  re-measured on regeneration, not carried forward.
- Metering is described as recording at the router with pricing at read time.
  Enforcement at the router is a proposal (ADR 0017) and the coverage gap is
  quoted with its measurement window; see `source-notes.md`.
- Slide rendering uses Liberation Sans or DejaVu Sans when Arial is unavailable,
  so rendered PNG metrics are indicative rather than pixel-identical to
  PowerPoint on macOS.
