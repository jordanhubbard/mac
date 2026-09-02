# MAC capabilities deck — `a168e9d0`

Point-in-time capabilities deck for the v1.3.5 release candidate, audited at
commit `a168e9d0` and captured 2026-09-02T13:13:14Z.

## Deck

Slides: [MAC — v1.3.5 release candidate (`a168e9d0`)](https://docs.google.com/presentation/d/16ZYljibDJ1toiyuBpKmxaiqSjsZ7j69bPDIuGB2tDH4/edit?usp=drivesdk)

The deterministic builder and exported Slides deck were verified at six slides.

Six slides, 16:9, describe the operational changes since v1.3.4: artifact
publication, integration-gate repair, fleet deploy resilience, AgentBus
visibility, the longer contract-test allowance, and the transactional release
workflow.

## Rebuild

```console
$ python3 -m venv /tmp/deckvenv
$ /tmp/deckvenv/bin/pip install python-pptx
$ /tmp/deckvenv/bin/python docs/presentation/20260902T131314Z-a168e9d0/build_deck.py
```

Publish with `scripts/publish-deck-to-slides.py` and `--expect-slides 6`.
Generated PNGs and PPTX files are ignored and are not committed.
