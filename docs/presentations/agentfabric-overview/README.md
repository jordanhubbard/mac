# AgentFabric overview — authoring package

This is the reproducible authoring package for the AgentFabric technical/marketing
overview: a `presentation` member (20-slide deck) and a `narrative` member (long-form
document) built from one reviewed source of claims.

The audience assumption is stated in the deck and must not drift: **a highly technical
reader who is new both to AgentFabric and to fleet-scale multi-agent / multi-model
orchestration.** Nothing in either member may assume prior product knowledge.

Narrative order, which is deliberate:

| Slides | What it argues |
| --- | --- |
| 1–5 | Marketing first: what the system does, why a chat window is not an operating model, the four properties it guarantees, and the end-to-end loop |
| 6–10 | Technology re-use: where AgentFabric leverages NVIDIA technology, where it leverages open source, the five mechanisms it adds anyway, and the containment boundary |
| 11–19 | How the machine actually runs: actors and their single authority each, task lifecycle, bus coordination, fleet shape, model routing and spend, evidence, measured surface area, and stated limits |
| 20 | Close and adoption path |

Diagrams and flows carry the argument. Bullet lists are used in one place on purpose —
the NVIDIA and open-source project inventories on slides 7 and 8 — because a list of
load-bearing dependencies is the point of those slides.

## Read in this order

1. [current deliverables](current-deliverables.md) — what exists right now
2. [deck specification](deck-specification.md) — slide-by-slide presentation authority
3. [narrative specification](narrative-specification.md) — long-form structure authority
4. [source notes](source-notes.md) — the factual ledger every claim traces to
5. [QA ledger](qa-ledger.md) — what was checked, and how to re-check it
6. [SKILL.md](SKILL.md) — the regeneration workflow

Model-facing inputs: [deck-authoring prompt](prompts/deck-authoring-prompt.md) and
[image prompts](prompts/image-prompts.md).

## Builders

| File | Role |
| --- | --- |
| [`build_deck.py`](build_deck.py) | deterministic `python-pptx` deck; native shapes only, no raster diagrams |
| [`build_narrative.py`](build_narrative.py) | deterministic `python-docx` narrative member |
| [`render_slides.py`](render_slides.py) | slide rasterizer, contact sheet, and text-frame overlap report |
| [`publish_google_workspace.py`](publish_google_workspace.py) | optional publication; requires explicit authorization |
| [`regenerate.sh`](regenerate.sh) / [`regenerate_python.sh`](regenerate_python.sh) | regeneration entry points |

Every diagram in the deck is built from native PowerPoint shapes, so the deck stays
editable and legible after a Google Slides import. There is no `assets/` directory and no
generated hero imagery: a mechanism diagram is preferred over decoration everywhere the
two compete.

Generated `.pptx` / `.docx` artifacts are build outputs. Regenerate them; do not treat a
stale copy as authority. Publication to any external location requires explicit
authorization from the owner of that location.
