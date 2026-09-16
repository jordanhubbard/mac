# MAC capabilities deck — `c7a3fee1`

Point-in-time capabilities deck for the v1.3.5 release, audited at commit
`c7a3fee1` and captured 2026-09-04T21:25:15Z.

## Deck

Slides: [MAC — v1.3.5 (`c7a3fee1`)](https://docs.google.com/presentation/d/11mrPpsYR-wzRTLYsCiKF3wWcniGP811D6s0zYgPIoV4/edit?usp=drivesdk)

The deterministic builder and exported Slides deck were verified at six slides.

Six slides, 16:9, describe the operational changes since v1.3.4: OpenShell/
OpenClaw onboarding fixes (reviewed-CLI preflight, degraded-gateway
tolerance, cold-pull gateway race, static sandbox binary linking), and fleet
dispatch/recovery fixes (targeted-task claiming, attestation reconciliation,
honest sandbox status, gateway-endpoint pinning).

## Rebuild

```console
$ python3 -m venv /tmp/deckvenv
$ /tmp/deckvenv/bin/pip install python-pptx
$ /tmp/deckvenv/bin/python docs/presentation/20260904T212515Z-c7a3fee1/build_deck.py
```

Publish with `scripts/publish-deck-to-slides.py` and `--expect-slides 6`.
Generated PNGs and PPTX files are ignored and are not committed.
