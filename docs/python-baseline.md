# Python baseline

MAC development, CI, native services and Linux OpenShell execution use CPython
**3.14.7**, the reviewed version in the root `.python-version` file. CI reads
that file instead of maintaining a separate Python matrix. Packaging rejects
older minor versions and unreviewed future minor versions.

For a development checkout, install the existing uv tool, then run:

```console
uv python install
"$(uv python find)" scripts/bootstrap-project.py
make test
```

Bootstrap checks the exact Python patch and installs the committed `uv.lock`
with the development extra. `MAC_VENV` still selects a custom environment.
Lint uses the prepared environment without synchronizing packages again.
Interpreter discovery does not download Python or synchronize dependencies.

A Python update is a reviewed source change, followed by a fleet rollout:

1. Update `.python-version`, the package requirement and the lockfile. Update
   the standalone onboarding constants and their receipt checks together;
   those helpers are uploaded individually before the repository is installed.
2. Update the checksum-verified uv assets when its Python catalog needs an
   update, and update the pinned multi-architecture Python/uv image digests.
   Container builds verify the actual interpreter against `.python-version`.
3. Run the full contract suite, generated-documentation checks and packaging
   smoke on the new interpreter inside Linux OpenShell. Preserve the existing
   network policy and independent publication workflow.
4. Provision the new managed interpreter before quiescing fleet services.
   Recreate service environments from the accepted source; preserve the prior
   interpreter and environment for rollback until canaries pass. Do not change
   system Python used by operating-system tools or recovery helpers.
5. Check the Python executable and package identity of each **running service**,
   including the separate Hermes environment. A matching command on PATH is
   insufficient. Complete a coding canary on every enabled fleet node through
   independent verification and publication, then release fleet holds.

Hermes has its own source and dependency contract. Its installed upstream
revision `9dd6634c5635321cf38840cc30e9b51226689128` declares `>=3.11,<3.14`.
`deploy/hermes/python314-source.json` identifies that exact source and the
before/after file hashes for `deploy/hermes/python314.patch`. The patch raises
the ceiling to `<3.15`, adds the published CPython 3.14 wheel hashes to the
lock, and adapts Hermes's daemon worker pool to CPython's changed worker
context. All 255 locked package versions are preserved. Explicitly select
Python 3.14.7 when preparing the environment; upstream's default remains 3.11.

Apply this patch only to a separate checkout of the manifest's upstream commit,
using `git apply --check` before `git apply`. Install from the patched lock with
`uv sync --locked --python 3.14.7` and the required extras. Do not ignore
`Requires-Python`, resolve new dependency versions, or patch a serving checkout.
The patch is compatibility material for the rollout; the gateway installer does
not apply it automatically. A fleet migration still needs a prepared replacement
environment, preserved profiles and gateway configuration, running-process
identity checks, and coding canaries before releasing holds.

The patch also repairs three upstream test observations without changing their
assertions: preserve the non-secret `TMPDIR` through the canonical runner's
credential-stripping environment; trace the actual pooled database reader; and
scope a simulated stat failure to the holder scan it tests. In the approved
Linux OpenShell sandbox, losing `TMPDIR` made real SQLite disk sorts fail on
both Python 3.12 and 3.14. The pooled-reader test exposed a WAL-mode observation
mistake; Python 3.14's changed `Path.exists()` behavior exposed the overly broad
stat mock. Runtime database repair and holder safety remain unchanged.

Compatibility evidence belongs to `task_dbc0cad0381a4f33824bcb7e873b5f24` in the
MAC ledger. A locked editable install, real terminal-tool dispatch, daemon-pool
regressions, and database/runner checks have passed on Python 3.14.7; focused
regressions also pass on 3.12.7. The full upstream suite has additional failures
in this sandbox, including failures reproduced on the older interpreter. Keep
those results visible: focused success is not a green full-suite result or
proof of a deployed fleet. The rollout remains separately tracked in
`task_7e957b6682ff463395894b881930fe78`.

The MAC runtime default does not override a different project's declared
interpreter requirements inside a task sandbox. Project-specific toolchains
remain isolated from the host service environment.

An independent verifier whose approved Linux image predates this baseline can
use `. scripts/bootstrap-verifier-python.sh` as its explicit bootstrap command.
This downloads checksum-pinned Python and uv through the image-owned curl with
the injected OpenShell CA, then uses image-owned pip to download hash-checked
wheels from `uv.lock`. Task-owned tools install and run offline. The helper
prepares development and documentation dependencies, removes temporary build
dependencies, and prevents `uv run` from changing packages during verification.
The normal full contract test follows it; network policy and review gates stay
in force. This bridge does not install an OpenShell runtime on a macOS host.

Native deployment uses that same lock with the `relay` and `postgres` extras.
Onboarding must provision the reviewed uv before services are quiesced. Before
moving the old source and environment, deployment inventories installed
packages, including unrecorded tools and their direct installation sources,
and merges the local footprint with the available hub replica. Local records
take precedence because workers write them before reporting to the hub. If the
hub is unavailable, an existing local record remains usable; a configured hub
failure with no local record stops the deployment instead of assuming no tools
were recorded.

The replacement environment carries `mac-runtime-lock.json` and
`mac-runtime-constraints.txt`. These bind core package versions to the source
lock and are restored with the environment by the existing generation rollback.
Tool restoration and later worker self-installs use those constraints. A missing
or changed receipt, changed core, or conflicting tool requirement fails closed;
redeploy the accepted locked runtime to repair drift. Compatible tools remain
supported. The inventory and resolver diagnostics can contain private package
URLs and are written to owner-private files; do not attach them to public logs.
