# Local edits to the vendored Hermes tree (`src/mac/_hermes`)

`src/mac/_hermes` is a pinned snapshot of upstream (see `../../src/mac/_hermes/SNAPSHOT_PIN`).
`scripts/vendor-hermes-snapshot.sh` re-vendors by copying **pristine upstream**
at the pinned commit and then re-applying the `*.patch` files in this directory.

**Anything edited directly inside `_hermes` that is NOT captured as a `.patch`
here will be silently lost on the next re-vendor.** The post-snapshot direct
edits below are now captured as `post-snapshot-mac-fixes.patch` so the vendoring
pipeline is idempotent again.

`tests/test_hermes_vendor_integrity.py` pins a content digest of the tree so any
further drift is caught by CI rather than discovered after a re-vendor.

## Direct edits made after the snapshot — captured in `post-snapshot-mac-fixes.patch`

| Commit | Summary | `_hermes` files touched |
|--------|---------|--------------------------|
| `1f62322` | Meter LLM usage per agent/task | `agent/agent_init.py` (X-MAC-{agent,task,lease} attribution headers — the fleet's billing-attribution mechanism) |
| `88ee3da` | Fix Slack thread participant triggers | `gateway/platforms/slack.py` |
| `e635c16` | Reconcile mac↔mac-dev ports | `agent/chat_completion_helpers.py`, `agent/tool_executor.py` |
| `526d50e` | Reconcile mac↔mac-dev tree alignment | `agent/tool_executor.py`, `hermes_cli/main.py` |

### How the patch was reconstructed and validated

The initial snapshot commit `48ac569` was itself produced by the vendor script
(pristine upstream @ pin + the then-existing `deploy/hermes/*.patch`, pruned), so
`git diff 48ac569 HEAD -- <these files>` **is** exactly the delta that must be
re-applied on top of the existing patches. It was generated with upstream-rooted
paths and validated to be well-formed and `-p1`-applicable, with its "after" side
matching the current tree:

```bash
git diff --relative=src/mac/_hermes 48ac569 HEAD -- \
  src/mac/_hermes/agent/agent_init.py \
  src/mac/_hermes/agent/chat_completion_helpers.py \
  src/mac/_hermes/agent/tool_executor.py \
  src/mac/_hermes/gateway/platforms/slack.py \
  src/mac/_hermes/hermes_cli/main.py \
  > deploy/hermes/post-snapshot-mac-fixes.patch

# validation (non-destructive): 'after' side must match current _hermes
git apply --check -R -p1 --directory=src/mac/_hermes \
  deploy/hermes/post-snapshot-mac-fixes.patch
```

**Ordering:** the vendor script applies `*.patch` in alphabetical (glob) order.
`post-snapshot-mac-fixes.patch` sorts after `multi-slack-mvp.patch`, which is
required because both touch `gateway/platforms/slack.py` and this patch's context
is the post-`multi-slack-mvp` state.

**Caveat (honest):** this was reconstructed against `48ac569` (the vendored
snapshot), not against a fresh network clone of pristine upstream — which was
unavailable in the environment where this was produced. It is exact under the
assumption that `48ac569` equals pristine-at-pin + the existing patches for these
files (which holds, since `48ac569` was produced by the vendor script). Before
the next real re-vendor, run `scripts/vendor-hermes-snapshot.sh` once end-to-end
and confirm the resulting tree matches the pinned digest in
`tests/test_hermes_vendor_integrity.py`.
