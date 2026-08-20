# MAC architecture deck — `bac50778`

A picture-first deck: how the hub relates to its workers, what one task does end to end, and what
runs inside the hub. Built by auditing the source at commit `bac50778`, captured
`2026-08-20T18:23:40Z`, with live console captures from a running fleet.

## The deck

**[MAC — How the control plane is put together (`bac50778`)](https://docs.google.com/presentation/d/1vzkNL3_IM-ophzQWUpJl3JE5L-X3MnEva8m6edeEOQk/edit)**

10 slides, 16:9, speaker notes on every slide. Six of the ten are pictures.

## Slides

| | |
|---|---|
| 1 | Title — commit and capture time |
| 2 | **One hub, many workers** — the core relationship, worker classes, AgentBus, the human on the bus |
| 3 | **The life of one task** — ten steps across five lanes, including the two failure edges |
| 4 | **Inside the hub** — three daemon threads, the tick in order, 31 services by purpose |
| 5 | **The fleet, observed** — live console, fleet tiles |
| 6 | **Movement, not a snapshot** — live task transitions and in-flight state |
| 7 | **Where changes actually land — and where they do not** — the merge queue at a 1% land rate |
| 8 | Scale at this commit |
| 9 | What is decided but not yet true |
| 10 | Provenance |

Slide 9 is deliberate. Eleven of twenty-four ADRs are still Proposed, ADR 0016 was accepted the day
this deck was built, and the merge queue is thrashing at a 1% land rate in plain sight on slide 7.

## What is checked in, and what is not

**Checked in: text only** — the three SVG diagram sources, this README, `AUDIT.md`, and the builder.

**Not checked in:** the rendered diagram PNGs, the console captures, and the built `.pptx`. All are
gitignored. `docs/` stays text-only so `tests/test_docs_no_operator_identity.py` keeps grepping
prose rather than compressed image bytes — see [`../README.md`](../README.md).

**The console captures cannot be regenerated from this repository.** They come from a hub that was
running when the deck was made. Rebuilding without a live fleet produces the diagrams and blank
screenshot slides. That is a property of the evidence, not a defect: a screenshot of a fleet is
true on a date, which is why the slides carry one.

## Rebuilding

Render the diagrams (headless Chrome, 2× device scale):

```console
$ CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
$ cd docs/presentation/20260820T182340Z-bac50778/images
$ "$CHROME" --headless --disable-gpu --hide-scrollbars \
    --force-device-scale-factor=2 --window-size=1600,940 \
    --default-background-color=FFFFFF \
    --screenshot=01-hub-and-workers.png "file://$PWD/01-hub-and-workers.svg"
```

Window sizes: `01` 1600×940 · `02` 1600×1010 · `03` 1600×900.

Capture the console from a live hub. The console takes a bearer token as `?t=`, and the views are
`live`, `stuck`, `agents`, `projects`, `pipelines`, `merge-queue`, `cycles`, `telemetry`:

```console
$ "$CHROME" --headless --disable-gpu --hide-scrollbars \
    --force-device-scale-factor=2 --window-size=1600,1050 --virtual-time-budget=18000 \
    --screenshot=05-console-live.png "http://<hub>:8789/ui?t=<token>&view=live"
```

**Crop the agents view before using it.** Its roster lists real agent names, and this repository's
docs must read as generic for any fleet owner. Only the top stat tiles are safe.

Then build and publish:

```console
$ python3 -m venv /tmp/deckvenv && /tmp/deckvenv/bin/pip install python-pptx
$ /tmp/deckvenv/bin/python docs/presentation/20260820T182340Z-bac50778/build_deck.py
$ scripts/publish-deck-to-slides.py \
    docs/presentation/20260820T182340Z-bac50778/mac-architecture-bac50778.pptx \
    --title "MAC — How the control plane is put together (bac50778)" --expect-slides 10
```

See `skills/cut-a-release/SKILL.md` for the full procedure and its traps.
