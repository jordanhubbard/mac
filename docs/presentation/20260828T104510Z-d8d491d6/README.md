# MAC capabilities deck — `d8d491d6`

A point-in-time capabilities deck for MAC, built by auditing the source and documentation at
commit `d8d491d6`, captured `2026-08-28T10:45:10Z`. This is the v1.3.0 release deck.

## The deck

**[MAC — What the control plane can do today (`d8d491d6`)](https://docs.google.com/presentation/d/1yOOzFqRVwhY6opljcPEzfkzQmdjwylsxi1_hFO_8wJ0/edit)**

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
fleet-identity tokens, and compressed image data eventually contains one by coincidence. Keeping
`docs/` text-only means that gate keeps reading prose, which is the only place identity can
actually leak.

(Which token is deliberately not written here. Naming it would put it in a checked-in doc and trip
the very gate this paragraph is about. Only the test file itself is exempt from the scan.)

## This is a pinned artifact

It describes `d8d491d6` and nothing later, and is **not** updated as the code moves. Build a new
sibling directory instead — see the convention in [`../README.md`](../README.md). An old deck should
keep meaning what it meant when it was shown.

Figures carry the date they were measured for the same reason. Token-routing numbers are still
ADR 0017's seven days to 2026-08-19. The ledger census in `AUDIT.md` is from 2026-08-28.

## Slides

1. Title — commit and capture time
2. What MAC is, and what it deliberately is not
3. *Part one: the model*
4. **One control plane, four objects** — the CLI as the object model, 125 verbs, 430 routes, six surfaces
5. **A task is a state machine with receipts** — 12 states and the four gates
6. *Part two: coordination and execution*
7. **A town square, not a switchboard** — the broadcast AgentBus, lifecycle verbs, dispatch, merge queue
8. **A heterogeneous fleet, honestly modelled** — node classes, coding-agent routes, images, secrets
9. *Part three: measurement*
10. **The fleet measures itself** — what it measures, the 2026-08-28 census, ADRs 0029 / 0017 / 0018
11. Scale at this commit
12. Proposed, deferred, or stale — stated up front
13. Provenance

Slide 12 is deliberate. Sixteen ADRs are still Proposed; ADR 0023 and 0033 are now Accepted;
ADR 0016 is Accepted as a decision while the hub still drives review; ADR 0012 remains deferred;
ADR 0031 was removed with CodeGraph; and the README still documents a vendored runtime that is
not in the tree (`AUDIT.md` §8). A capabilities deck that omits those is marketing.

## Rebuilding and re-publishing

Render the diagrams to PNG with headless Chrome at 2× device scale:

```console
$ CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
$ cd docs/presentation/20260828T104510Z-d8d491d6/images
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
$ /tmp/deckvenv/bin/python docs/presentation/20260828T104510Z-d8d491d6/build_deck.py
```

Publish with `scripts/publish-deck-to-slides.py` and `--expect-slides 13`. The rendered PNGs and the
built `.pptx` are ignored by git, so a rebuild leaves the working tree clean.
