# MAC v1.5.0 capabilities

[Download the public eight-slide deck](https://github.com/jordanhubbard/mac/releases/download/v1.5.0/mac-v1.5.0-capabilities.pptx).
[Google Slides](https://docs.google.com/presentation/d/1i9PUkXG1iPDeU1AmDqxUM439zwdtXqbDkTQikGIRyNA/edit?usp=drivesdk) is account-restricted: Google rejected public sharing
with `publishOutNotPermitted`. Its conversion was verified by authenticated
export and an eight-page count. The public release distributes the original,
locally built PPTX independently of Google.

Source candidate: `c7be3a5abb299ba409cad31d469883cd52284969`. Captured 2026-09-14. The publication metadata
is layered over this immutable source commit. See [the claim audit](AUDIT.md)
and [the complete release audit](../../releases/v1.5.0-audit.md).

## Slides

1. Verify before publication.
2. A durable request-to-result path.
3. Hermes conversation and MAC execution.
4. Isolate concurrent verification.
5. All testing happens up front.
6. Tests must detect the old failure.
7. Documentation follows the source.
8. Release scope and remaining limits.

## Rebuild

Copy the builder into a Node.js workspace with `@oai/artifact-tool` installed. The builder
uses editable native text and has no image dependencies. Run it from a workspace
that can resolve that package:

```console
node build_deck.mjs
```

Outputs go to the system temporary directory under `mac-v1.5.0-slides`;
`MAC_DECK_OUT` may select a different output directory. Rendered PNGs, layout
JSON and the PPTX are generated artifacts and are not committed. Inspect every
rendered slide before republishing. The repository publisher accepts the PPTX
with `--expect-slides 8` and verifies the converted page count.
