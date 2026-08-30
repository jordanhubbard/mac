# Image prompts — intentionally empty

This package generates no images, and there is no `assets/` directory.

Every diagram in the AgentFabric overview deck is built from native PowerPoint
shapes in `build_deck.py`: rounded rectangles, chevrons, chips, connectors,
arrows, and text frames. That is a deliberate constraint rather than a
limitation of the toolchain:

- The deck survives a Google Slides import as an editable object graph, so a
  reviewer can move a box or fix a label without regenerating an image.
- A diagram that renders as text and shapes stays searchable, diffable in the
  builder, and legible when projected badly.
- A generated illustration cannot be traced to an authority in
  `source-notes.md`, so it can only carry mood — and this deck earns attention
  with mechanism instead.

If a future slide genuinely needs a raster asset, add the prompt here, record
why a native diagram could not carry the claim, and note the decision in
`qa-ledger.md`. Do not add decorative hero imagery or image-only slides;
`deck-specification.md` prohibits both.
