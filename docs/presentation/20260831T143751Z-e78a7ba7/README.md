# MAC capabilities deck — `e78a7ba7`

Point-in-time capabilities deck for MAC v1.3.4, audited at commit `e78a7ba7` and captured 2026-08-31T14:37:51Z.

## Deck

Slides: [MAC — v1.3.4 capabilities (`e78a7ba7`)](https://docs.google.com/presentation/d/1uPIlC_TYrp3XHd4ARIbrdxNjPiYgAE7pjdi2n_2FUD8/edit)

13 slides, 16:9, with speaker notes. The checked-in SVGs and `build_deck.py` reproduce the untracked PPTX.

## Release focus

This deck retains the complete control-plane capabilities view and pins the v1.3.4 release changes: explicit Git 2.38 prerequisites, honest partial-coverage reporting, self-provisioned PostgreSQL test gates, PostgreSQL 17 docs CI, host-Python upgrade recovery, and bounded clock-skew handling for lease telemetry.

## Rebuild

```console
$ python3 -m venv /tmp/deckvenv
$ /tmp/deckvenv/bin/pip install python-pptx
$ /tmp/deckvenv/bin/python docs/presentation/20260831T143751Z-e78a7ba7/build_deck.py
```

Publish with `scripts/publish-deck-to-slides.py` and `--expect-slides 13`. Generated PNGs and PPTX files are ignored and not committed.
