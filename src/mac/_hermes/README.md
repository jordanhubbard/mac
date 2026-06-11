# Vendored Hermes runtime

This directory is a **pinned, pruned, owned snapshot** of the Hermes agent
runtime (`NousResearch/hermes-agent`), vendored per
[ADR 0001](../../../docs/adr/0001-unify-hermes-runtime-into-mac.md). It replaces
the old deploy behavior of cloning pristine upstream and applying patches +
runtime string-surgery.

- **Pin / provenance**: see `SNAPSHOT_PIN` here and
  [`deploy/hermes/SNAPSHOT.md`](../../../deploy/hermes/SNAPSHOT.md) for the
  commit, the measured include/exclude manifest, the dependency merge, and the
  re-vendor procedure.
- **Do not edit upstream files by hand to chase upstream.** Re-vendor with
  `scripts/vendor-hermes-snapshot.sh` (bumps the pin as a reviewed act). The
  three former out-of-tree patches are folded in at vendor time.
- **Importing it**: this tree holds Hermes' *flat top-level* packages (`agent`,
  `gateway`, `hermes_cli`, `tools`, `plugins`, `providers`, ...). Call
  `mac.hermes_vendor.ensure_on_path()` before importing any of them — it puts
  this directory on `sys.path` so they import unchanged (no namespace rewrite).
- **Not vendored here** (cruft pruned): `website/`, `ui-tui/`, `web/`, `tests/`,
  `docs/`, `nix/`, `packaging/`, `infographic/`, `locales/`, datagen, etc.
</content>
