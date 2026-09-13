# Python baseline

MAC development, CI, native services and Linux OpenShell execution use CPython
**3.14.7**, the reviewed version in the root `.python-version` file. CI reads
that file instead of maintaining a separate Python matrix. Packaging rejects
older minor versions and unreviewed future minor versions.

For a development checkout, install the existing uv tool, then run:

```bash
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

Hermes has its own source and dependency contract. The installed revision and
upstream source inspected on 2026-09-13 declare `>=3.11,<3.14`; this is a rollout
prerequisite to resolve and test, not a reason to ignore `Requires-Python`.
MAC's version policy does not by itself prove Hermes compatibility or migrate
its environment. Preserve personality, memory and gateway configuration during
that migration. The task ledger records the compatibility work and rollout.

The MAC runtime default does not override a different project's declared
interpreter requirements inside a task sandbox. Project-specific toolchains
remain isolated from the host service environment.
