# MAC trusted certifier image

This image is the independent test station for managed candidate
certification.
It is intentionally separate from worker and hub images:

- it receives only an exact, content-addressed Git bundle;
- it has no hub token, Git remote, repository credential, or landing authority;
- OpenShell runs it as `sandbox` with no network and hard Landlock enforcement;
- `/opt/mac-certifier/bin/run-contract-tests` is owned by the image, not the
  candidate;
- the launcher verifies a frozen manifest of the complete reviewed `tests/`
  tree, plugin test, and every test/coverage control before candidate Python is
  imported;
- the independent verdict executes the root-owned `/opt` baseline tests and
  conftest directly against candidate `src/`; the sandbox UID cannot chmod,
  unlink, or replace those controls;
- a supplemental isolated-clone phase retains candidate source, deploy files,
  docs, and plugin implementation while replacing candidate tests/config with
  the frozen baseline, providing candidate-file checks without
  being mistaken for the root-owned integrity boundary;
- a trusted `sitecustomize` places the exact candidate `src/` first for the
  parent test process and its Python children.

Both certifier pytest harnesses (`authoritative-contract-tests` and
`supplemental-contract-tests`) launch pytest through `env -i` with an explicit,
sanitized environment and never pass `-n`/`--dist`. They run a single serial
pytest owner with no `PYTEST_XDIST_*` inheritance, so they cannot start a
nested xdist controller or fan out a zero-collection worker pool; the
`scripts/run-contract-tests.sh` single-owner and nested exit-5 handling is the
only place that guard is needed.

The controller appends the exact immutable `assembly_base_sha` as
`--base-sha SHA`; repository contracts cannot provide or override it. The
frozen selector proves that object exists in the bundle and is an ancestor of
the candidate, then scopes `base...HEAD` (never `HEAD^`, which is ambiguous for
assembly merge commits). Execution is proportional:

- docs/media-only changes run only root-owned invariant tests;
- mapped `src/` changes run invariant plus frozen module tests;
- unmapped source changes run one root-owned full suite;
- deploy/config/root-visible changes run focused root-owned tests and one
  supplemental full suite;
- an unmapped source change mixed with a root-visible change is rejected and
  must be split, because satisfying both scopes would require two full suites.

The structured `mac.certifier_phase_manifest.v1` line records the base,
candidate, changed-path digest, exact phase/test plan, and `full_suite_count`.
The controller validates and includes it in the durable certification result
and station receipt. `full_suite_count` can never exceed one. Global coverage
is not repeated here; the candidate pre-push and tested-main gates already
record it.

The pinned `python` and `uv` OCI indexes, `uv.lock`, BuildKit provenance, SBOM,
and immutable registry digest make a publication auditable. Debian packages are
still obtained from the pinned base image's configured snapshot at build time;
the registry digest and provenance are the final byte-level identity.

## Build locally

Docker Buildx is required. A local build never publishes:

```bash
scripts/build-certifier-image.sh
docker run --rm ghcr.io/jordanhubbard/mac-certifier:local \
  /opt/mac-certifier/bin/run-contract-tests --image-self-test
```

The image build requires a clean, exact Git revision by default. Use
`--allow-dirty` only for local development; a dirty image is tagged `local` and
is not a valid activation artifact. Before Docker receives the context,
`scripts/certifier-context-manifest.py` rejects symlinks and secret-shaped
Git-visible files, records a digest of every included path/content/mode tuple,
and materializes only that audited allowlist into a new build directory. Git-
ignored and other local files therefore cannot reach `COPY .`, even when their
names do not match a known secret pattern. The certifier-specific Docker ignore
file remains defense in depth for credentials, keys, per-run evidence, and
environment files.

## Publication and activation

The tested main-branch CI workflow publishes all deployment images from
the same tested commit using only the repository's ephemeral `GITHUB_TOKEN`
with `packages: write`. It publishes:

- `ghcr.io/jordanhubbard/mac:git-<40-char-commit>`
- `ghcr.io/jordanhubbard/mac-openshell-runtime:git-<40-char-commit>`
- `ghcr.io/jordanhubbard/mac-certifier:git-<40-char-commit>`

The certifier job runs only when its frozen inputs change (or on a manual CI
run). Updating `.mac/project.yaml` with a resulting digest therefore cannot
cause a new certifier digest and an endless contract-update loop.

Never place the tag in the repository contract. Copy the exact
`ghcr.io/jordanhubbard/mac-certifier@sha256:<digest>` value from the workflow's
`certifier-image-publication` artifact, then verify it before the atomic
contract update:

```bash
scripts/verify-certifier-publication.py \
  ghcr.io/jordanhubbard/mac-certifier@sha256:<64-lowercase-hex> \
  --expected-revision <40-character-tested-main-sha>
```

That command uses a new empty `DOCKER_CONFIG` to prove anonymous registry
read-back of the exact digest, verifies the OCI revision and non-root user,
runs the image self-test with networking disabled, and prints the checksum of
`src/mac/openshell/default-policy.yaml`. This is mandatory because the remote
OpenShell gateway intentionally has no GHCR credentials.
