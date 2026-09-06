# MAC capabilities deck — `a787bff1`

Point-in-time capabilities deck for the v1.4.0 release, audited at commit
`a787bff1` and captured 2026-09-06T05:13:11Z.

## Deck

Slides: [MAC — v1.4.0 (`a787bff1`)](https://docs.google.com/presentation/d/1VX5AkOBjjz4X2DsUynYazO4ok9KuFXz7CjQN785HVkY/edit?usp=drivesdk)

The deterministic builder and exported Slides deck were verified at six slides.

Six slides, 16:9, describe the fleet's chat-gateway reliability work since
v1.3.5: three OpenClaw hardening fixes (cron schedule collision, a host-side
flock mutex, message-body encoding), the filesystem-level root cause that
made those insufficient (broken POSIX locking on a Docker Desktop overlayfs
mount, reported upstream), the cutover of all three fleet nodes' chat gateway
from OpenClaw back to Hermes (shell-installed, not vendored, not pip-installed),
and a real regression caught live in production during that cutover (a
config-writer that silently discarded a working model provider configuration,
fixed with dotted-path keys).

## Rebuild

```console
$ python3 -m venv /tmp/deckvenv
$ /tmp/deckvenv/bin/pip install python-pptx
$ /tmp/deckvenv/bin/python docs/presentation/20260906T051311Z-a787bff1/build_deck.py
```

Publish with `scripts/publish-deck-to-slides.py` and `--expect-slides 6`.
Generated PNGs and PPTX files are ignored and are not committed.
