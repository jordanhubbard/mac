# MAC capabilities deck — `8b424c20`

A point-in-time capabilities deck for MAC, built by auditing the source and documentation at
commit `8b424c20`, captured `2026-08-20T01:12:24Z`.

| | |
|---|---|
| **Deck** | `mac-capabilities-8b424c20.pptx` — 13 slides, 16:9, speaker notes on every slide |
| **Audit** | `AUDIT.md` — every claim traced to a file, commit or generated reference |
| **Diagrams** | `images/*.svg` (sources) and `images/*.png` (2× renders) |
| **Builder** | `build_deck.py` |

## This is a pinned artifact

It describes `8b424c20` and nothing later, and it is **not** updated as the code moves. Build a new
sibling directory instead — see the convention in [`../README.md`](../README.md). An old deck should
keep meaning what it meant when it was shown.

Figures carry the date they were measured for the same reason. The token-routing and ledger-census
numbers were true for the seven days to 2026-08-19; the dreaming figures cited in `AUDIT.md` are
from 2026-07-28.

## Slides

1. Title — commit and capture time
2. What MAC is, and what it deliberately is not
3. *Part one: the model*
4. **One control plane, four objects** — the CLI as the object model, and six surfaces onto it
5. **A task is a state machine with receipts** — 11 states and the four gates between them
6. *Part two: coordination and execution*
7. **A town square, not a switchboard** — the broadcast AgentBus, lifecycle verbs, dispatch, merge queue
8. **A heterogeneous fleet, honestly modelled** — node classes, coding-agent routes, images, secrets
9. *Part three: measurement*
10. **The fleet measures itself** — what it measures, what it found, and the three ADRs that resulted
11. Scale at this commit
12. Proposed, deferred, or stale — stated up front
13. Provenance

Slide 12 is deliberate. ADRs 0016–0018 are **Proposed**, not shipped; ADR 0012 is Accepted with
implementation deferred; and the repository README still documents a vendored Hermes runtime that
no longer exists in the tree (`AUDIT.md` §8). A capabilities deck that omits those is marketing.

## Regenerating

The diagrams are hand-authored SVG. PNGs are rendered with headless Chrome at 2× device scale:

```console
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
cd docs/presentation/20260820T011224Z-8b424c20/images
"$CHROME" --headless --disable-gpu --hide-scrollbars \
  --force-device-scale-factor=2 --window-size=1520,900 \
  --default-background-color=FFFFFF \
  --screenshot=01-object-model.png "file://$PWD/01-object-model.svg"
```

Window sizes: `01` 1520×900 · `02` 1520×880 · `03` 1520×900 · `04` 1520×900 · `05` 1520×930.

The deck itself needs `python-pptx`, which is deliberately **not** a repository dependency — this is
a documentation artifact, not part of the shipped runtime:

```console
python3 -m venv /tmp/deckvenv && /tmp/deckvenv/bin/pip install python-pptx
/tmp/deckvenv/bin/python docs/presentation/20260820T011224Z-8b424c20/build_deck.py
```

## Presenting from Google Slides

Upload `mac-capabilities-8b424c20.pptx` to Drive and open it with Google Slides, or use
**File → Import slides**. Full-bleed diagram images and speaker notes both survive the conversion.
