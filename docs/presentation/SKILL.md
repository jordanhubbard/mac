---
name: regenerate-hgx-runner-google-doc
description: Regenerate and verify the HGX-Runner architecture Google Doc from current MAC, Literate AI, and HGX/Horde authority.
---

# Regenerate the HGX-Runner Google Doc

This directory is the durable authoring package for the native Google Doc named
in `current-deliverables.md`.

Before changing or regenerating the document:

1. Read the repository-root `AGENTS.md`, this file, and
   `skills/author-presentations-and-documents/SKILL.md` completely.
2. Use CodeGraph first when a source repository has `.codegraph/` and code
   behavior must be located or verified.
3. Treat `document-specification.md` as narrative authority and
   `source-notes.md` as the factual-claim ledger.
4. Preserve the target decision: HGX-Runner is the durable destination; MAC is
   ported, migrated, cut over, and retired. Do not turn current MAC maturity into
   an argument for retaining MAC as a second control plane.

## Refresh procedure

1. Revalidate each consequential claim against current source and record the
   exact repository revisions in `source-notes.md`.
2. Update the specification and factual ledger before the Python authoring source.
3. Run `python scripts/update_google_doc.py --check`.
4. Create a temporary Google Docs copy and run `./regenerate.sh --document-id ID
   --apply`. Never use the canonical document as the first render target.
5. Inspect Google's exported PDF as a complete contact sheet and inspect every
   diagram or dense page at full size. Correct clipping, overflow, broken tables,
   unreadable text, awkward page breaks, unsupported claims, and stale caveats.
6. Update the canonical Google Doc only after the temporary copy passes. Read it
   back and render it again.
7. Record both content review and visual review in `qa-ledger.md`. Trash the
   temporary copy after final verification.

## Guardrails

- Use native Google Docs paragraphs, headings, lists, links, and tables. Do not
  introduce a DOCX intermediate into this workflow.
- Keep the six diagrams editable as native Docs tables; do not rasterize them.
- Never persist OAuth tokens or raw credential-bearing command output.
- Keep API responses, PDFs, PNGs, and contact sheets outside the repository.
- Preserve current/partial/proposed labeling and cover negative paths.
- A polished diagram cannot promote a roadmap item into implemented behavior.
- A permanent HGX-to-MAC facade or dual-write design is not the destination.
