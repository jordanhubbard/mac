---
name: agentfabric-overview-presentation
description: Regenerate or extend the AgentFabric overview document pair (20-slide deck plus prose narrative) from this authoring package. Read before editing any file in docs/presentations/agentfabric-overview/.
---

# AgentFabric overview presentation

This package produces two members of one document pair from source: a 20-slide
presentation and a prose narrative. Both are generated; neither is hand-edited.
If you find yourself editing the `.pptx` or the `.docx`, stop — edit the builder.

## Sources of truth, in precedence order

| File | Owns |
| --- | --- |
| `source-notes.md` | every factual claim, its authority in the tree, and its implemented / decided / proposed status |
| `deck-specification.md` | slide order, per-slide visual form, required claims, prohibitions |
| `narrative-specification.md` | narrative section order and depth |
| `build_deck.py` | the deck; the only way slides are produced |
| `build_narrative.py` | the narrative; the only way the document is produced |
| `render_slides.py` | PNG rendering, geometry checks, text-frame overlap detection |
| `verify_pair.py` | pair invariants: slide count, notes coverage, heading spine, manifest honesty |
| `qa-ledger.md` | what was run, when, and what it said |

A claim that is not in `source-notes.md` does not go on a slide. If you need a new
claim, add it there **with its authority first**, then use it.

## Regenerating

```console
docs/presentations/agentfabric-overview/regenerate.sh
```

That runs, in order: `build_deck.py`, `build_narrative.py`, `render_slides.py`,
`verify_pair.py`. It requires `python-pptx`, `python-docx`, `lxml`, and `Pillow`
importable from `$OBJ_DIR/doc-toolchain`, `.venv`, or `python3`; it will not
install them for you. Set `AGENTFABRIC_DECK_SKIP_RENDER=1` to skip rendering
while iterating on prose.

Generated artifacts (`agentfabric-overview.pptx`, `agentfabric-overview.docx`)
live beside the builders. QA output — rendered PNGs, contact sheet, manifest,
acceptance record — lands in the ignored `_build/agentfabric-overview/`.

## Rules that are not negotiable

1. **Diagrams are native shapes.** Every diagram is built from PowerPoint
   shapes, connectors, and text frames so the deck stays editable after a Google
   Slides import. No raster diagrams, no decorative hero imagery, no image-only
   slides.
2. **Speaker notes on every slide.** `verify_pair.py` fails the build otherwise.
   Notes carry the mechanism and the qualifications that do not fit on the slide.
3. **Bullets are for inventories only.** Lists are allowed where the content
   genuinely is a list of NVIDIA or OSS projects. Everywhere else, use a diagram
   or a flow.
4. **Status is never blurred.** Implemented, decided-but-not-yet-runtime, and
   proposed stay visibly distinct, and each item's placement traces to an ADR's
   own status line.
5. **No invented numbers.** No ROI, productivity, throughput, or maturity
   figures. Counted figures carry their command and their measurement date, and
   are re-measured on every regeneration rather than carried forward.
6. **Re-use is stated as re-use.** A dependency is named as a dependency; a
   design reference is named as a reference. NemoClaw is a reference, not the
   deployed gateway.
7. **Publication requires explicit authorization.** `publish_google_workspace.py`
   has no default destination and requires `--slides-id` and `--authorized-by`.
   Do not publish, and do not record a published location in the manifest,
   without that authorization from the repository owner.

## Extending the deck

Adding or reordering slides means changing three files together:
`deck-specification.md` (the contract), `build_deck.py` (the implementation), and
`verify_pair.py`'s `EXPECTED_SLIDES` (the gate). Update `qa-ledger.md` with the
run that proved it, and re-measure any count you touched.

Keep the narrative in step: if a claim changes on a slide, the corresponding
narrative section changes in the same commit. Divergence between the two members
on a shared claim is a defect, not a stylistic difference.
