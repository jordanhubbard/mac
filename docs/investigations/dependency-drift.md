# Dependency mutation during interpreter discovery

On 2026-09-12, a separate Linux OpenShell clone reproduced the dependency
change first observed between the bulk and protected phases of the PR #803
contract gate. The repository bootstrap installed AnyIO 4.15.1. One call to
`scripts/fault-replay.py::_probe_interpreter` changed it to 4.13.0 in the same
virtual environment. The package manager reported 24 packages removed and
24 installed, while the existing interpreter test still passed.

The helper ran `uv run python -c "import sys; print(sys.executable)"` from the
repository root before checking existing interpreters. That command could
synchronize the project environment to `uv.lock`. The bootstrap uses
`pip install -e .[dev]`, which does not consume that lock. The helper's test
therefore let interpreter discovery change dependencies underneath other
tests sharing the environment.

The repair removes package-manager invocation from discovery. It checks the
existing project environment and then the running interpreter, accepting a
candidate only when it can import `psycopg`. A missing driver still produces
an actionable failure. CI can explicitly select and prepare its environment
with `uv run` before starting the replay; discovery itself performs no package
installation or synchronization.

Regression coverage exercises discovery with an available package manager,
both with and without a usable project environment, and verifies that
installed state remains unchanged. Existing tests retain the missing-driver,
fallback, and actual probe-execution contracts.

A separate acceptance probe used a fresh pip-bootstrapped Linux environment
with the real package manager available. AnyIO remained at 4.15.1; installed
package versions and the content digest of 6,998 non-bytecode environment
files were identical before and after discovery. The only executed command
was the selected interpreter's `import psycopg` check.

This reproduction identifies a mechanism consistent with the historical
version discrepancy. It does not trace the historical process or establish
that no other command changes dependencies. It also does not make the
bootstrap lock-aware; that is a separate reproducibility concern.

The investigation is `task_ffd712b7e4f949bca66e4636dc845446`, published as
`pub_ef8251714a6f41009eeae1a16663b148`. Reproduction evidence
`ev_2f41ac59377c4fe78075909bf51e3e33` contains the exact probe and complete log.
The implementation is `task_b0f54d9c3b4ecfaf020ade8a652dba0b`.
