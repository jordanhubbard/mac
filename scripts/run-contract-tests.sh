#!/usr/bin/env bash
set -euo pipefail

# Fleet executors inherit deployment/task environment. Keep repository tests
# hermetic so they exercise the checked-out code, not the live agent runtime.
unset "${!ACC_@}"
unset "${!FIRECRAWL_@}"
unset "${!HERMES_@}"
unset "${!MAC_@}"
unset "${!QDRANT_@}"
unset "${!SLACK_@}"
unset "${!TOKENHUB_@}"

# Hermetic HOME: unsetting env vars is not enough — a deployed host also carries
# real fleet config under ~/.hermes, ~/.mac and ~/.config, which leaked into
# "hermetic" tests (e.g. the Slack adapter read the live workspace config) and
# made the suite fail in the contract sandbox on any host but the one it was
# authored on. Redirect HOME/XDG to a throwaway dir, seeded with a minimal git
# identity so git-touching tests still work. Cleaned up on exit.
_MAC_TEST_HOME="$(mktemp -d 2>/dev/null || echo "${TMPDIR:-/tmp}/mac-contract-home.$$")"
mkdir -p "$_MAC_TEST_HOME/.config"
trap 'rm -rf "$_MAC_TEST_HOME"' EXIT
export HOME="$_MAC_TEST_HOME"
export XDG_CONFIG_HOME="$_MAC_TEST_HOME/.config"
git config --global user.email "mac-contract-tests@example.invalid" >/dev/null 2>&1 || true
git config --global user.name "mac contract tests" >/dev/null 2>&1 || true
# The suite has tests that shell out to git (git ls-files, etc.). When the repo
# is a clone owned by a different uid than the test runner — e.g. the hub
# verifier uploads a clone into an OpenShell sandbox run as the `sandbox` user —
# git refuses with "detected dubious ownership" and those git-using tests fail
# spuriously (the 4 test_fleet_samples failures that blocked hub-side review
# verification). Trust the checkout regardless of ownership; this is a
# throwaway hermetic HOME, so the wildcard is scoped to this run only.
git config --global --add safe.directory '*' >/dev/null 2>&1 || true

# Resolve a usable interpreter instead of assuming a repo-local .venv. Local
# dev / bare-metal hosts have .venv; the OpenShell task sandbox ships the mac
# runtime at /opt/mac-venv and has no .venv (a missing .venv/bin/python is
# exactly the rc-127 that blocked every repo-coupled code task from passing
# in-sandbox verification). pytest's pythonpath=["src"] (pyproject) makes the
# checked-out worktree shadow any installed mac, so tests exercise the
# worktree's code regardless of which interpreter runs them.
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
elif [ -x "/opt/mac-venv/bin/python" ]; then
    PY="/opt/mac-venv/bin/python"
else
    PY="$(command -v python3 || command -v python || true)"
fi
if [ -z "${PY}" ]; then
    echo "run-contract-tests.sh: no python interpreter found (.venv, /opt/mac-venv, or PATH)" >&2
    exit 1
fi

# The resolved interpreter must actually carry the suite's toolchain: the dev
# extras (coverage, pytest) AND the project deps. A worker host's PATH python
# is its runtime venv — mac's runtime deps but no dev extras — and verification
# there died with "No module named coverage" (observed live on the GKE pods,
# blocking every repo-change task); a dev host's bare python3 can have the
# opposite hole (global pytest/coverage, no project deps). Probe both classes
# cheaply; when the interpreter can't run the suite and the repo ships its
# bootstrap, build the hermetic .venv the execution contract already promises.
_py_can_run_suite() {
    "$1" -m coverage --version >/dev/null 2>&1 \
        && "$1" -m pytest --version >/dev/null 2>&1 \
        && "$1" -c "import cryptography, fastapi, yaml" >/dev/null 2>&1
}
if ! _py_can_run_suite "$PY"; then
    if [ ! -x ".venv/bin/python" ] && [ -f "scripts/bootstrap-project.py" ]; then
        echo "run-contract-tests.sh: $PY cannot run the suite; bootstrapping .venv" >&2
        "$PY" scripts/bootstrap-project.py --venv-only >&2
    fi
    if [ -x ".venv/bin/python" ] && _py_can_run_suite ".venv/bin/python"; then
        PY=".venv/bin/python"
    else
        echo "run-contract-tests.sh: no interpreter can run the suite (tried $PY and .venv;" \
             "need coverage+pytest+project deps)" >&2
        exit 1
    fi
fi
export PATH="$(cd "$(dirname "$PY")" && pwd):${PATH}"

if [ "$#" -eq 0 ]; then
    "$PY" -m coverage erase
    "$PY" -m coverage run -m pytest
    exec "$PY" -m coverage report
fi

exec "$PY" -m pytest "$@"
