# MAC capabilities deck — `8b424c20`

A point-in-time capabilities deck for MAC, built by auditing the source and documentation at
commit `8b424c20`, captured `2026-08-20T01:12:24Z`.

## The deck

**[MAC — What the control plane can do today (`8b424c20`)](https://docs.google.com/presentation/d/1DXgpB-3fy4IDLynGloaAP349BWrwoVSw8T46VyPdT3M/edit)**

13 slides, 16:9, speaker notes on every slide. Published as a native Google Slides presentation
rather than committed as a binary — see "What is checked in, and what is not" below.

## What is in this directory

| File | Purpose |
|---|---|
| `AUDIT.md` | Every claim in the deck traced to a file, commit or generated reference |
| `images/*.svg` | The five diagrams, hand-authored, and the durable source for them |
| `build_deck.py` | Rebuilds the `.pptx` from the diagrams so it can be re-published |
| `README.md` | This file |

## What is checked in, and what is not

**Checked in: text only.** The SVG diagram sources are the durable artifact — they diff, they
review, and they regenerate everything else.

**Not checked in: the rendered PNGs and the built `.pptx`.** Both are generated outputs, and
committing them would put megabytes of opaque binary under `docs/` for no reviewable gain. It also
breaks a gate: `tests/test_docs_no_operator_identity.py` greps every tracked file under `docs/` for
fleet-identity tokens, and compressed image data eventually contains one by coincidence — the first
PNG tried here matched one of them inside its pixel data. Keeping `docs/` text-only means that gate
keeps reading prose, which is the only place identity can actually leak.

(Which token is deliberately not written here. Naming it would put it in a checked-in doc and trip
the very gate this paragraph is about. Only the test file itself is exempt from the scan.)

## This is a pinned artifact

It describes `8b424c20` and nothing later, and is **not** updated as the code moves. Build a new
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
implementation deferred; and at this commit the repository README still documented a vendored
Hermes runtime that no longer existed in the tree (`AUDIT.md` §8). A capabilities deck that omits
those is marketing.

## Rebuilding and re-publishing

Render the diagrams to PNG with headless Chrome at 2× device scale:

```console
$ CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
$ cd docs/presentation/20260820T011224Z-8b424c20/images
$ "$CHROME" --headless --disable-gpu --hide-scrollbars \
    --force-device-scale-factor=2 --window-size=1520,900 \
    --default-background-color=FFFFFF \
    --screenshot=01-object-model.png "file://$PWD/01-object-model.svg"
```

Window sizes: `01` 1520×900 · `02` 1520×880 · `03` 1520×900 · `04` 1520×900 · `05` 1520×930.

Then build the deck. `python-pptx` is deliberately **not** a repository dependency — this is a
documentation artifact, not part of the shipped runtime:

```console
$ python3 -m venv /tmp/deckvenv && /tmp/deckvenv/bin/pip install python-pptx
$ /tmp/deckvenv/bin/python docs/presentation/20260820T011224Z-8b424c20/build_deck.py
```

Upload the result to Drive and open it with Google Slides, or use **File → Import slides**.
Full-bleed diagram images and speaker notes both survive the conversion. The rendered PNGs and the
built `.pptx` are ignored by git, so a rebuild leaves the working tree clean.
