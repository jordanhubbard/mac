#!/usr/bin/env bash
set -euo pipefail

_MAC_TEST_PORTFOLIO_REQUESTED="${MAC_TEST_PORTFOLIO:-0}"

# Fleet executors inherit deployment/task environment. Keep repository tests
# hermetic so they exercise the checked-out code, not the live agent runtime.
unset "${!ACC_@}"
unset "${!FIRECRAWL_@}"
unset "${!HERMES_@}"
unset "${!MAC_@}"
unset "${!QDRANT_@}"
unset "${!SLACK_@}"
unset "${!TOKENHUB_@}"
# Git-forge credentials: worker hosts MUST carry these to publish, but
# gitops.token_for_host() consults them, so a live GH_TOKEN/GITHUB_TOKEN
# changes inject_git_remote_auth() behavior under test — one leaked token
# failed test_ref_host_token_auth_and_redaction_edges on every fleet host
# while the same suite passed on tokenless dev machines and hub sandboxes.
unset GH_TOKEN GITHUB_TOKEN GITEA_TOKEN GIT_TOKEN

# Coding-agent route detection (coding_agent._route_fields) fingerprints each
# CLI route from provider endpoint/model/credential env vars, several of which
# are NOT MAC_-prefixed and so survive the unset "${!MAC_@}" sweep above. A
# fleet worker / codex-runner host carries a live OPENAI_BASE_URL (the mac
# router endpoint) and API keys; those leaked into "hermetic" detection tests
# and shifted the codex route to provider=mac-router, so the heartbeat
# inventory fingerprint no longer matched the probe report and
# test_worker_falls_through_failed_claude_and_publishes_verified_codex saw
# "unverified" instead of "verified" — passing on tokenless dev machines and
# hub sandboxes but failing on every host with a configured coding route.
# Clear the non-prefixed provider knobs so route fingerprints are built from a
# clean baseline identically on all hosts.
unset OPENAI_BASE_URL OPENAI_API_KEY
unset ANTHROPIC_BASE_URL ANTHROPIC_MODEL ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY
unset CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX CLAUDE_CODE_USE_FOUNDRY
unset CURSOR_API_KEY CURSOR_AGENT_ENDPOINT

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
# Stock git defaults new repos to `master`; most dev machines set
# init.defaultBranch=main in their user gitconfig, which this hermetic HOME
# deliberately hides. Pin it so tests that build repos behave identically on
# dev machines and fleet sandboxes (a bare-canonical clone checked out nothing
# on stock git and failed two publication tests only on fleet hosts).
git config --global init.defaultBranch main >/dev/null 2>&1 || true
# The suite has tests that shell out to git (git ls-files, etc.). When the repo
# is a clone owned by a different uid than the test runner — e.g. the hub
# verifier uploads a clone into an OpenShell sandbox run as the `sandbox` user —
# git refuses with "detected dubious ownership" and those git-using tests fail
# spuriously (the 4 test_fleet_samples failures that blocked hub-side review
# verification). Trust the checkout regardless of ownership; this is a
# throwaway hermetic HOME, so the wildcard is scoped to this run only.
git config --global --add safe.directory '*' >/dev/null 2>&1 || true

# The merge-gate suite (and the production merge queue) uses
# `git merge-tree --write-tree`, added in git 2.38. On an older git the
# git-publication/merge-queue tests fail with an opaque rc=129 — lead the log
# with the actual cause so a fleet host running distro git (e.g. 2.34 on the
# GKE pod image) is diagnosable from the first line of the failure output.
_git_ver="$(git version 2>/dev/null | sed -E 's/^git version ([0-9]+)\.([0-9]+).*/\1 \2/')"
if [ -n "${_git_ver}" ]; then
    _git_major="${_git_ver%% *}"
    _git_minor="${_git_ver#* }"
    if [ "${_git_major:-0}" -lt 2 ] || { [ "${_git_major:-0}" -eq 2 ] && [ "${_git_minor:-0}" -lt 38 ]; }; then
        echo "run-contract-tests.sh: WARNING: $(git version) < 2.38 —" \
             "merge-gate tests (and the production merge queue) WILL fail;" \
             "upgrade git on this host" >&2
    fi
fi

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
    portfolio_dir=""
    if [ "$_MAC_TEST_PORTFOLIO_REQUESTED" = "1" ]; then
        portfolio_dir="$(pwd)/.test-portfolio"
        rm -rf "$portfolio_dir"
        mkdir -p "$portfolio_dir"
        export COVERAGE_FILE="$portfolio_dir/.coverage"
        export MAC_TEST_PORTFOLIO_OUTPUT="$portfolio_dir/timings.json"
    fi
    "$PY" -m coverage erase
    # `patch = ["subprocess"]` in pyproject.toml makes the parent and every
    # Python child (including pytest-xdist workers) write parallel coverage
    # data, so a single `coverage combine` merges every process's data set —
    # this is what lets the run below split into two pytest invocations while
    # still enforcing one coverage total.
    #
    # Parallelism: xdist runs the bulk unit/CLI/API/UI slice across cores
    # (~5x wall-clock here). Tests marked process_e2e/postgres/container run
    # SERIALLY in a second invocation: they bind real ports and spawn real
    # processes, so concurrent scheduling would collide. --dist loadscope
    # keeps a module's tests (and its module-scoped fixtures) on one worker.
    #
    # MAC_TEST_JOBS controls worker count: unset/"auto" => one per core;
    # "0" => disable xdist entirely (serial), the fallback for memory- or
    # core-constrained hosts. Portfolio mode always runs serial so per-test
    # timing/coverage attribution stays exact.
    _MAC_TEST_JOBS="${MAC_TEST_JOBS:-auto}"
    _SERIAL_MARK="process_e2e or postgres or container_contract or docker_e2e"
    pytest_status=0
    if [ "$_MAC_TEST_PORTFOLIO_REQUESTED" != "1" ] && [ "$_MAC_TEST_JOBS" != "0" ]; then
        "$PY" -m coverage run -m pytest -n "$_MAC_TEST_JOBS" --dist loadscope \
            -m "not ($_SERIAL_MARK)" || pytest_status=$?
        if [ "$pytest_status" -eq 0 ]; then
            "$PY" -m coverage run -m pytest -m "$_SERIAL_MARK" || pytest_status=$?
        fi
    else
        "$PY" -m coverage run -m pytest || pytest_status=$?
    fi
    "$PY" -m coverage combine
    "$PY" -m coverage json -o coverage.json
    report_status=0
    "$PY" -m coverage report || report_status=$?
    policy_status=0
    "$PY" scripts/coverage-policy.py --coverage-json coverage.json || policy_status=$?
    portfolio_status=0
    if [ -n "$portfolio_dir" ]; then
        "$PY" scripts/test-portfolio.py --output-dir "$portfolio_dir" --report-only \
            || portfolio_status=$?
    fi
    if [ "$pytest_status" -ne 0 ]; then
        exit "$pytest_status"
    fi
    if [ "$report_status" -ne 0 ]; then
        exit "$report_status"
    fi
    if [ "$portfolio_status" -ne 0 ]; then
        exit "$portfolio_status"
    fi
    exit "$policy_status"
fi

"$PY" -m pytest "$@"
