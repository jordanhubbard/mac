---
name: "document-pair"
description: "Ecosystem document-pair construction. Use for Literate AI workflow tasks."
metadata:
  author: "Literate AI maintainers <literate-ai-maintainers@users.noreply.github.com>"
schema: "urn:literate-ai:schema:v1:specification-to-source-skill"
skill_id: "document-pair"
version: "1.0.0"
title: "Ecosystem document-pair construction"
stages:
  - "generate"
dependencies: []
limitations:
  - "Do not publish to an external destination, contact a collaboration service, or widen an access audience beyond the declared value."
  - "Do not write credentials, access tokens, or refresh material into the generated source tree, the authoring package, or the capability manifest."
  - "Do not rasterize text to satisfy a layout constraint, and do not replace native objects with rendered images."
  - "Do not invent metrics, promote a roadmap outcome into an implemented claim, or state a claim absent from the factual ledger."
  - "Do not write plans, caches, rendered previews, or layout JSON into the generated source tree."
trust: "repository-reviewed"
---
# Ecosystem document-pair construction

Generate the build source that realizes a document pair for exactly one selected
`documentation.ecosystem` target, from the consuming Component's specification and the
ecosystem binding fragment contributed by the selected Flavor.

Emit a deterministic build program that constructs every declared member as native
editable objects, plus the capability manifest described by the document-pair interface.
Read the member set, surface geometry, access audience, permission, and link-sharing
state from the consuming specification; read format, collaboration surface, access
vocabulary, and the concrete formatting mapping from the ecosystem binding fragment.
Never infer either from the other.

Realize only local artifacts. Set every member's `published_location` to null and leave
publication to a separately authorized step. Record the resolved access record for each
member without contacting the service that would enforce it.

For every realized presentation member, author non-empty per-page notes carrying that
page's supporting authority and its recorded limits, and keep every element's bounding
box inside the declared surface geometry. For every realized narrative member, use the
ecosystem's built-in heading styles without skipping a level.

Generate the acceptance evidence the document-pair acceptance contract consumes: a
per-page geometry scan against the declared surface, a notes-coverage count, a heading
hierarchy walk, a placeholder scan, and a credential scan over the manifest and every
retained artifact. Emit these as machine-readable results, not prose. They are lifecycle
metadata; do not read them back from the build program at runtime.

Treat the authoring package as an input to be preserved, never rewritten: the narrative
specification, factual ledger, generation prompts, owned assets, regeneration entry
point, deliverable links, and QA record are authored material. Only the build source is
generated.
