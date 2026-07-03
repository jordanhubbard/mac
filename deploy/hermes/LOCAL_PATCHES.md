# Local delta of the vendored Hermes tree (`src/mac/_hermes`)

`src/mac/_hermes` is a pinned snapshot of upstream (see
`../../src/mac/_hermes/SNAPSHOT_PIN`). `scripts/vendor-hermes-snapshot.sh`
reproduces it from **pristine upstream at the pinned commit** via three deltas,
which together were validated to reproduce the committed tree **byte-for-byte**
(a fresh pristine clone → all patches → prune → copy INCLUDE → remove dashboard
bundles → copy overlay → `diff` clean):

1. **Patches** — `deploy/hermes/*.patch`, `git apply`-ed to the upstream tree in
   alphabetical order. These are text-source edits to files that survive into
   the vendored surface, so they ride on top of upstream and conflict loudly if
   upstream moves.
2. **Removals** — the pipeline deletes `plugins/*/dashboard/dist` (built
   dashboard bundles that MAC does not vendor; upstream ships them, so a
   re-vendor would otherwise reintroduce them).
3. **Overlay** — `deploy/hermes/overlay/` holds MAC-**authored** files that are
   not part of the upstream surface. `rm -rf "$DEST"` at the start of a re-vendor
   drops them, so they are copied back in after the upstream copy.

`tests/test_hermes_vendor_integrity.py` pins a content digest of the tree so any
future drift — a hand edit or a re-vendor — fails loudly rather than silently.

## Patch set

| Patch | Purpose |
|-------|---------|
| `disable-shutdown-chat-notices.patch` | (pre-existing) |
| `mac-provider-decision.patch` | (pre-existing) |
| `mac-runtime-context-prompt.patch` | (pre-existing) |
| `multi-slack-mvp.patch` | (pre-existing) |
| `post-snapshot-mac-fixes.patch` | **reconstructed** — the 10 text-source edits that had been made directly to the vendored tree without a patch (X-MAC billing-attribution headers in `agent/agent_init.py`, the Slack thread-trigger fix, reconciliation edits, and other de-personalization/runtime edits across `agent/`, `gateway/`, `cron/scheduler.py`, `tools/{code_execution,todo}_tool.py`, `toolsets.py`). Sorts last so it applies after `multi-slack-mvp.patch` (both touch `gateway/platforms/slack.py`). |

## Overlay files (`deploy/hermes/overlay/`)

MAC-authored, not in upstream — dropped by `rm -rf "$DEST"` on re-vendor unless
copied back:

- `README.md` (the "this tree is vendored, re-vendor with the script" doc)
- `plugins/image_gen/mac-hub/{__init__.py,plugin.yaml}` (the mac-hub image-gen plugin)
- `plugins/image_gen/nvidia/{__init__.py,plugin.yaml}` (the NVIDIA image-gen plugin)
- `tools/fleet_tool.py`, `tools/embedding_tool.py` (MAC agent tools)

## How the patch was reconstructed and validated

The reconstruction diffed the **real pipeline output** (pristine@pin + the
existing patches, pruned to the vendored surface) against the committed tree —
the initial-snapshot commit turned out **not** to be byte-identical to
pristine+patches (de-personalization altered more files than the post-snapshot
commits), so an earlier attempt that diffed against the snapshot commit was
wrong and was discarded. `.vendor-check.sh` (temporary; not committed) performed
the reconstruction and the end-to-end validation against a fresh network clone
of pristine upstream. It printed `IDENTICAL`, i.e. the three deltas fully
reproduce the committed `_hermes`.

Before the next pin bump, re-run `scripts/vendor-hermes-snapshot.sh --apply` end
to end and confirm the resulting tree still matches the digest in
`tests/test_hermes_vendor_integrity.py` (it will fail loudly if not).
