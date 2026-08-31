# QA ledger — AgentFabric overview

What was run, what it said. Append a dated entry per regeneration; do not edit
past entries. An entry without a command and its output is not evidence.

---

## 2026-08-30 — initial AgentFabric edition

Package created by adapting the Literate-AI `docs/presentations/` authoring
package to AgentFabric, then replacing the deck, the narrative, and all
specifications. Built against `jordanhubbard/mac` at `2976182`.

### Build

```
docs/presentations/agentfabric-overview/regenerate.sh
```

- `build_deck.py` — `built 20 slides -> docs/presentations/agentfabric-overview/agentfabric-overview.pptx`
- `build_narrative.py` — `built narrative -> docs/presentations/agentfabric-overview/agentfabric-overview.docx (67 headings)`
- `render_slides.py` — 20 PNGs plus contact sheet, `no text-frame overlaps detected`
- `verify_pair.py` — `pair verified: 20 slides, notes on all of them, 67 narrative headings`

### Gates

| Gate | Result |
| --- | --- |
| Slide count matches specification (20) | pass |
| Speaker notes present on every slide (20/20) | pass |
| Text-frame overlap detection | pass — none detected |
| Narrative heading spine, no skipped levels | pass — 67 headings, 0 skips |
| Manifest claims no unauthorized publication | pass — both members `publication_authorized: false`, `published_location: null` |
| Stale-source references (`literate`, `litai`, `component://`, case-insensitive) | pass — the only remaining matches are the provenance notes in this ledger |
| Native diagrams only, no `assets/` directory | pass — package contains no images |
| No invented ROI / productivity / throughput figures | pass — reviewed slide-by-slide against `source-notes.md` |
| `make lint` (ruff check + format --check) | pass for this package (14 files, check clean, format clean). The repository-wide run also reports 15 pre-existing unformatted files under `tests/`, none of them touched here |
| `scripts/run-contract-tests.sh` | documentation contract and environment registry pass; 11,136 tests pass, 44 fail. All 44 are the merge-queue / git-publication families the runner itself warns about on a host with git 2.34.1 (`git merge-tree` requires ≥ 2.38); unrelated to this package, which adds no runtime code paths |
| `scripts/test-docs.py` fence contract | pass — `console` fences only under `docs/`; a `bash` fence in `SKILL.md` was rejected and changed |
| `scripts/generate-docs-reference.py --write` | regenerated; `docs/reference/documentation-inventory.md` now lists the nine package documents |

### Defects found and fixed during this pass

1. **Slide 13 overlap.** The terminal/held chip row collided with the
   `ALSO TERMINAL OR HELD:` label. Chips were shifted right and narrowed; the
   re-render reports no overlaps.
2. **Linux font fallback missing.** `render_slides.py` only searched macOS font
   paths and silently fell back to PIL's bitmap default, producing an unreadable
   contact sheet. Liberation Sans and DejaVu Sans paths were added.
3. **Slide 18 counts were inherited, not measured.** The copied deck's figures
   (219 modules, 430 routes, 125 CLI verbs) did not match the tree. Re-measured
   to 221 modules, 435 routes, 458 CLI leaf commands, 12 task states; commands
   recorded in `source-notes.md`.
4. **Metering was overstated.** The deck claimed every model call is metered at
   the router and listed router-side metering as implemented. ADR 0017 is
   *Proposed* and quantifies a 29.5 per cent unmetered fraction over the seven
   days to 2026-08-19. Slides 4, 16, and 19 now say route events are recorded and
   priced at read time, with enforcement listed as proposed and the coverage gap
   quoted with its window.
5. **Named gates were overstated.** Slide 9's claim now carries a speaker note
   distinguishing the implemented review gate (ADR 0011, Accepted) from the
   general contract (ADR 0022, Proposed).
6. **Publisher pointed at another project's deck.** `publish_google_workspace.py`
   defaulted to the Literate-AI Slides ID and named the Literate-AI narrative.
   The default was removed; `--slides-id` and `--authorized-by` are now required.
7. **Regeneration referenced a missing verifier.** Both regenerate scripts called
   `scripts/verify_document_pair.py` and a `components/literate-ai-overview/`
   component file, neither of which exists in this repository. Replaced with the
   package-local `verify_pair.py`; the unused Codex `build_deck.mjs` fallback path
   was removed.

### Visual review

Contact sheet inspected at `_build/agentfabric-overview/contact-sheet.png`.
Confirmed: marketing-first opening, NVIDIA and OSS re-use inventories, unique
mechanisms, trust boundary, actor roles, task lifecycle, AgentBus coordination,
heterogeneous fleet, route ladder, evidence flow, honest status columns, adoption
close. No slide is bullets-only except the two deliberate project inventories.

### Publication

Not published, not authorized. No Google Slides or Docs resource exists for this
package.
