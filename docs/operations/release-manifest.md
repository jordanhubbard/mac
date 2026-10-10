# The release manifest

A matching Git HEAD doesn't describe a deployed MAC system. The Python lock,
uv, Hermes, OpenShell, the coding and verification images, the service
templates and the database schema can each differ while the checkout agrees.
The release manifest names all of them for one exact commit.

`mac.release_manifest` builds it from the source tree and the worker runtime
image's CI publication receipt, and from nothing else: no host, network or
installed package is consulted. Two builds of one commit therefore agree.

| Section | What it pins |
| --- | --- |
| `mac` | commit, tree and version |
| `python` | the reviewed interpreter (`.python-version`), `requires-python`, and the `pyproject.toml` and `uv.lock` digests |
| `tools` | reviewed native tools (uv, Python) and the digest of `deploy/reviewed-tool-assets.sh` |
| `runtime_tools` | the worker image's frozen build arguments (OpenCode, Claude Code, Node, pnpm, gh, Rust, buildx), from `scripts/image-publication-identity.py` |
| `hermes` | the upstream repository and revision, the source manifests and the patch digests (`mac.hermes_release`) |
| `openshell` | the reviewed CLI, gateway and supervisor version, and the asset, bootstrap and policy digests |
| `images` | one image for coding **and** verification, by digest, from the receipt |
| `services` | digests of every service template, wrapper and installer (`deploy/systemd`, `deploy/bin`, the macOS and fleet-context installers, `scripts/fleet-update`) |
| `config_schema` | the generated environment registry digest |
| `database` | the migration count and latest migration |
| `roles` | which components each role installs, and on which platforms |

Roles are the only allowed differences. The hub is `darwin/arm64` and installs
no OpenShell or images. Workers are `linux/arm64` and `linux/amd64`, and the
manifest doesn't claim their artifacts are byte-identical.

The manifest carries `manifest_sha256` over its canonical JSON, so it is
immutable by content. A manifest whose image pin is unresolved has
`complete: false` and fails `verify`.

## Where it comes from

On every push to `main`, the CI job that publishes the worker runtime image
builds the manifest from that commit and the image's publication receipt, and
uploads it as the `mac-release-manifest` artifact (kept 90 days). The build
fails if any pin is open or the receipt belongs to another commit.

## Commands

```console
PYTHONPATH=src python3 -m mac.release_manifest build --source . \
  --runtime-receipt openshell-runtime-publication/publication-receipt.json \
  --require-complete --output mac-release-manifest.json
python3 -m mac.release_manifest verify mac-release-manifest.json
python3 -m mac.release_manifest diff old.json new.json   # dotted paths that changed
```

`build` refuses a tree with uncommitted changes, a tree not at `--commit`, a
reviewed Python that disagrees with `.python-version`, and a receipt that isn't
a passed receipt for this commit's worker runtime image.
