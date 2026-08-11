---
name: author-presentations-and-documents
description: Create, revise, and regenerate evidence-backed slide decks and technical documents from a Literate-AI project's current authority. Use when a user asks for PowerPoint, Google Slides, Word, Google Docs, an architecture or roadmap document, an executive narrative, or another polished project artifact that must stay synchronized with specifications and code.
metadata:
  author: Literate AI maintainers <literate-ai-maintainers@users.noreply.github.com>
---

# Author presentations and documents

Create project artifacts from current authority, not from memory or stale marketing copy.

## Workflow

1. Read the root `AGENTS.md`, root `SKILL.md`, and declared documentation spine. When
   `.codegraph/` exists, use CodeGraph before broad text search to locate implementation
   evidence.
2. Define the communication job: audience, decision or understanding required, central
   takeaway, output format, publication target, and source constraints. Honor the
   user's explicit ecosystem preference first. Otherwise use a selected
   `documentation.ecosystem` Flavor when one is declared. If neither decides the
   destination, retain an editable local artifact and ask only when publishing matters.
3. Separate current behavior, current limitations, near-term investment, and long-term
   direction. Never present a roadmap outcome as implemented or invent metrics.
4. Separate each consequential claim from the mechanism that makes it true, and carry
   both. A claim asserts an outcome; a mechanism names the artifacts, identities, rules,
   and ordering that produce it. An artifact whose every page states outcomes reads as
   promotion regardless of how accurate it is.
5. Use a dedicated presentation or document capability when available, but never make a
   proprietary MCP, connector, or one user's installed agent plugin a hidden requirement.
   Otherwise use a declared project-local toolchain such as Node with a presentation or
   document library, or Python with an OOXML library. Keep its package manifest and lock
   with the authoring package; install only into an ignored environment under `OBJ_DIR`
   after the user authorizes installation. Preserve editable native objects and use
   generated imagery only when it materially improves the explanation.
6. Create a durable authoring package beneath the project's declared documentation roots
   or its documented artifact convention. Preserve the narrative specification, factual
   source ledger, generation prompts, build source, owned assets, regeneration entry
   point, current deliverable links, and QA record needed to reproduce the artifact.
7. Render every slide or page. Inspect the complete contact sheet plus dense or
   image-led pages at full size. Fix unintended overlap, clipping, overflow, broken
   connectors, unreadable type, unresolved placeholders, and unsupported claims.
8. Run the content review in "Review depth before layout" before treating the artifact
   as complete, and record its result in the QA record alongside the visual findings.
9. Export the local editable artifact beneath the project's output convention. A local
   artifact and reproducible package are the portable baseline. If a selected
   `documentation.ecosystem` Flavor requests publication, read that Flavor's complete
   binding before touching a service. The binding owns tool discovery, optional
   installation, authentication, update semantics, access mapping, and read-back
   verification. Publish only when the user authorizes the exact destination; record its
   link without embedding credentials.

## Keep the portable baseline honest

- No MCP is mandatory. A connector may accelerate publication, but the authoring package
  must remain reproducible without that connector.
- Detect required format libraries, fonts, and renderers before authoring. Prefer existing
  `PATH` tools. If a required tool is absent, either request authorization for a
  project-local installation or return the exact missing prerequisite and installation or
  login action. Never silently weaken native-object, notes, geometry, or visual-QA gates.
- Keep package closure isolated beneath `OBJ_DIR`; never place `node_modules`, virtual
  environments, rendered pages, or office-conversion state in the repository source tree.
- A missing renderer may block visual acceptance even when structural OOXML checks pass.
  Report that distinction explicitly rather than claiming completion.

## Carry mechanism, not only claim

- Show the durable authority itself at least once. An artifact arguing that a readable
  specification is the product must display a real specification excerpt. An artifact
  arguing that evidence ships with the result must display a real receipt, plan, diff,
  inventory, or command transcript. Never argue for readable intent without showing it.
- Prefer the rule with its consequences over the adjective. "Exact" and "governed" are
  assertions; the identities a key binds, the identities it deliberately refuses to bind,
  and the invalidation blast radius that follows are the evidence.
- Cover the negative path. An artifact that only renders the success sequence invites the
  reader's first objection and answers none of it. Show what happens when a gate fails,
  what is invalidated, what is retained, and what must run again.
- Answer cost and containment when the audience owns budget or risk. Concurrency bounds,
  budget decisions, reuse predicates, and the boundary material never crosses are
  routinely the first questions asked and the last topics authored.
- Spend page budget on distinct ideas. Restating one lifecycle in several visual forms
  consumes the room that unaddressed subjects need; a depth deficit and a breadth deficit
  are usually the same missing pages.
- When one artifact serves both a decision audience and an implementing audience, keep a
  claim-led main sequence and carry the supporting mechanism in a clearly separated
  annex, appendix, or reference section rather than diluting either.

## Carry the boundaries into the artifact

- Author speaker notes or equivalent per-page prose for every page. State the supporting
  authority, the limits recorded in the factual ledger, and what the page does not claim.
- Assume the artifact will be forwarded without its author. A caveat that survives only
  in the authoring package or in live narration is not carried by the artifact.
- Keep the ledger and the notes consistent. When a boundary changes in the ledger, update
  every page whose notes depend on it.

## Review depth before layout

Answer these before the artifact is complete. Record each answer in the QA record.

- Could a skeptical practitioner in the audience reconstruct how the system works from
  this artifact alone, or only that its authors believe it works?
- Which pages state a mechanism, and which only assert an outcome? Report the ratio.
- Is the project's central subject shown as a concrete artifact anywhere?
- Which pages restate an idea another page already made? Justify or remove each.
- Which questions this audience will certainly ask go unanswered?
- Does every quantified figure trace to the factual ledger, and does every figure
  presented as measured actually measure the property under discussion?

## Preserve project boundaries

- Treat presentations and technical documents as maintained documentation artifacts, not
  Component source and not generation-prompt authority.
- Keep generated previews, layout JSON, temporary conversions, and access tokens outside
  the repository.
- Preserve user-owned templates, branding, and unrelated working-tree changes.
- Link every consequential claim to a specification, current implementation, retained
  evidence, or an explicitly labeled future assumption.
- A polished artifact cannot grant build, execution, publication, or release authority.
- Documentation Flavors choose an artifact ecosystem, not content authority. Do not
  silently rewrite claims, audience, or approval state while converting formats.
