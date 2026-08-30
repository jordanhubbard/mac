#!/usr/bin/env bash
set -euo pipefail

_MAC_TEST_PORTFOLIO_REQUESTED="${MAC_TEST_PORTFOLIO:-0}"
# Worker count for the xdist-safe bulk slice. Empty => a headroom-aware default
# (~2/3 of cores, min 2) computed once the interpreter is resolved. That is a
# big jump from the old fixed 2 while retaining CPU and memory headroom for the
# suite's real subprocesses and containers on heterogeneous hosts. Test
# semantics must never depend on winning a scheduler race; explicit values
# still win: MAC_TEST_JOBS=auto (one per reported core), MAC_TEST_JOBS=<N>
# (pin), MAC_TEST_JOBS=0 (serial, for memory- or core-constrained hosts).
_MAC_TEST_JOBS_REQUESTED="${MAC_TEST_JOBS:-}"
# Contract-runner tests (and the outer merge gate) invoke this script from an
# already-running pytest process.  Starting a SECOND xdist controller from
# inside a pytest/xdist worker is unsupported in the confined OpenShell
# executor: the nested controller fans out a second worker pool that collects
# zero items and exits 5 even though the same tests pass directly.  Remember the
# outer-runner markers before the hermetic environment sweep so that when nested
# we can (1) force a single serial owner — NEVER a second controller or a
# zero-collection child pool — and (2) distinguish a legitimate empty outer
# selection (pytest exit 5) from a real collection error or test failure.  Any
# ONE of the standard pytest/xdist worker markers proves we are nested; xdist
# sets PYTEST_XDIST_WORKER on workers and PYTEST_XDIST_TESTRUNUID on both the
# controller and the workers, and PYTEST_CURRENT_TEST is set while any test
# runs, so a serial (non-xdist) outer pytest is caught too.
_MAC_TEST_NESTED_PYTEST=0
if [ -n "${PYTEST_CURRENT_TEST:-}" ] \
    || [ -n "${PYTEST_XDIST_WORKER:-}" ] \
    || [ -n "${PYTEST_XDIST_WORKER_COUNT:-}" ] \
    || [ -n "${PYTEST_XDIST_TESTRUNUID:-}" ]; then
    _MAC_TEST_NESTED_PYTEST=1
fi
# Coverage is the repository's merge-gate safety rail (statement/branch floors),
# but it also dominates full-suite wall-clock. Rollout VERIFICATION and local
# dev loops only need pass/fail, so MAC_TEST_COVERAGE=0 runs the same suite with
# the same xdist/serial split but without coverage tracing or the floor policy —
# much faster and lighter. Coverage stays ON by default so the merge gate is
# unchanged; do NOT set MAC_TEST_COVERAGE=0 for the pre-push/merge gate.
_MAC_TEST_COVERAGE_REQUESTED="${MAC_TEST_COVERAGE:-1}"
# Namespaces (pytest markers) an operator can switch OFF with one flag, e.g.
# MAC_TEST_DISABLE_GROUPS=fleet,heavy_e2e. The conftest hook deselects the
# matching tests so they cost zero wall-clock — handy to smoke-verify a rollout
# without the slow real-subprocess clusters. Deselecting a namespace removes ITS
# coverage, so it is honored ONLY on the non-gating fast path; the guard below
# hard-refuses it whenever coverage is on (the merge gate must run every
# contract). Captured before the hermetic MAC_* sweep, re-exported for pytest.
_MAC_TEST_DISABLE_GROUPS_REQUESTED="${MAC_TEST_DISABLE_GROUPS:-}"
if [ -n "$_MAC_TEST_DISABLE_GROUPS_REQUESTED" ] \
    && { [ "$_MAC_TEST_COVERAGE_REQUESTED" != "0" ] || [ "$_MAC_TEST_PORTFOLIO_REQUESTED" = "1" ]; }; then
    echo "run-contract-tests.sh: MAC_TEST_DISABLE_GROUPS is only honored in fast mode (set MAC_TEST_COVERAGE=0, non-portfolio); the merge gate must run every contract" >&2
    exit 2
fi
# Impact-based gate: with MAC_TEST_SELECT_BASE=<ref> the runner asks
# scripts/resolve-impacted-tests.py for the tests whose code actually changed
# vs <ref>. A 'full' resolution (or any resolver error) falls through to the
# whole-repo gate — fail-closed. A 'focused' resolution runs only those tests
# and, when coverage is on, enforces the CHANGED-LINE floor (diff-coverage)
# because whole-repo totals are not measurable from a subset. This is the
# days->hours rollout win; the whole-repo floors are re-enforced by the
# scheduled full run. Unset => the default full gate below is byte-identical.
_MAC_TEST_SELECT_BASE_REQUESTED="${MAC_TEST_SELECT_BASE:-}"
# Scheduled full run: after a passing portfolio gate, rebuild the committed
# test-impact map from the fresh per-test coverage so selection stays fresh.
_MAC_TEST_REBUILD_MAP_REQUESTED="${MAC_TEST_REBUILD_MAP:-0}"
# Checkpointing: with MAC_TEST_CHECKPOINT=1 the runner records which test files
# passed and, on a later invocation, carries those results forward instead of
# re-running the whole suite for a one-line fix. The rules (what invalidates a
# checkpoint, what may be skipped, and why coverage floors are NEVER computed
# from a resumed subset) are in src/mac/test_checkpoint.py. Default 0, so the
# merge gate is byte-identical unless an operator or CI opts in.
_MAC_TEST_CHECKPOINT_REQUESTED="${MAC_TEST_CHECKPOINT:-0}"
_MAC_TEST_CHECKPOINT_DIR_REQUESTED="${MAC_TEST_CHECKPOINT_DIR:-}"
# Pre-baked runtime venv location for interpreter resolution, captured here
# because the hermetic MAC_* sweep below unsets every MAC_-prefixed var. Tests
# point this at a nonexistent path so their staged fake python3 (on PATH) is
# the interpreter that resolves, instead of a real host /opt/mac-venv silently
# winning and running the whole suite. Empty/unset => the /opt/mac-venv default.
_MAC_CONTRACT_RUNTIME_VENV_REQUESTED="${MAC_CONTRACT_RUNTIME_VENV:-}"
# The suite runs against Postgres, so its DSN must survive the MAC_* sweep --
# it is test configuration, not inherited fleet configuration. Without this the
# sweep silently removes it and every test fails with "MAC_TEST_PG_URL is
# unset", pointing at the CI provisioning step rather than at this line.
_MAC_TEST_PG_URL_REQUESTED="${MAC_TEST_PG_URL:-}"
_MAC_COVERAGE_DIR=""

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
if [ -n "$_MAC_TEST_PG_URL_REQUESTED" ]; then
    export MAC_TEST_PG_URL="$_MAC_TEST_PG_URL_REQUESTED"
else
    # Nobody provisioned a database. CI does, and a developer following
    # CLAUDE.md does, but a task sandbox runs this gate directly with no
    # opportunity to -- so it failed with "MAC_TEST_PG_URL is unset" no matter
    # how complete the sandbox was. Provisioning is this gate's own business:
    # the helper finds a running server, or a container engine, or starts a
    # server from installed binaries, and says so on stderr when it cannot.
    _pg_helper="$(dirname "$0")/start-test-postgres.sh"
    if [ -x "$_pg_helper" ] && _pg_dsn=$("$_pg_helper"); then
        eval "$_pg_dsn"
    fi
fi
# Pytest configuration belongs to this repository and the explicit arguments
# passed to this runner.  In particular, an inherited ``-n auto`` must not make
# the protected process/container phase concurrent after the runner separates
# it from the xdist-safe bulk phase.
unset PYTEST_ADDOPTS PYTEST_CURRENT_TEST PYTEST_XDIST_WORKER
unset PYTEST_XDIST_WORKER_COUNT PYTEST_XDIST_TESTRUNUID
unset COVERAGE_FILE COVERAGE_PROCESS_START

# Coding-agent route detection (coding_agent._route_fields) fingerprints each
# CLI route from provider endpoint/model/credential env vars, several of which
# are NOT MAC_-prefixed and so survive the unset "${!MAC_@}" sweep above. A
# fleet worker / codex-runner host carries a live OPENAI_BASE_URL (the mac
# router endpoint) and API keys (OPENAI_API_KEY plus the codex-specific
# CODEX_API_KEY that _detect_codex also honors); those leaked into
# "hermetic" detection tests and shifted the codex route to
# provider=mac-router, so the heartbeat
# inventory fingerprint no longer matched the probe report and
# test_worker_falls_through_failed_claude_and_publishes_verified_codex saw
# "unverified" instead of "verified" — passing on tokenless dev machines and
# hub sandboxes but failing on every host with a configured coding route.
# Clear the non-prefixed provider knobs so route fingerprints are built from a
# clean baseline identically on all hosts.
unset OPENAI_BASE_URL OPENAI_API_KEY CODEX_API_KEY
unset ANTHROPIC_BASE_URL ANTHROPIC_MODEL ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY
unset CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX CLAUDE_CODE_USE_FOUNDRY
unset CURSOR_AUTH_TOKEN CURSOR_API_KEY CURSOR_AGENT_ENDPOINT

# Hermetic HOME: unsetting env vars is not enough — a deployed host also carries
# real fleet config under ~/.hermes, ~/.mac and ~/.config, which leaked into
# "hermetic" tests (e.g. the Slack adapter read the live workspace config) and
# made the suite fail in the contract sandbox on any host but the one it was
# authored on. Redirect HOME/XDG to a throwaway dir, seeded with a minimal git
# identity so git-touching tests still work. Cleaned up on exit.
_MAC_TEST_HOME="$(mktemp -d 2>/dev/null || echo "${TMPDIR:-/tmp}/mac-contract-home.$$")"
mkdir -p "$_MAC_TEST_HOME/.config"
cleanup_contract_test_runtime() {
    rm -rf "$_MAC_TEST_HOME"
    if [ -n "$_MAC_COVERAGE_DIR" ]; then
        rm -rf "$_MAC_COVERAGE_DIR"
    fi
}
trap cleanup_contract_test_runtime EXIT
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
#
# That last sentence holds for SUBPROCESSES only because the repository
# conftest.py re-exports those ini entries into PYTHONPATH. The ini option by
# itself edits the pytest process's sys.path and nothing else, which is why the
# sandbox once ran in-process assertions against the worktree and shelled-out
# assertions against the mac baked into /opt/mac-venv. See _export_ini_pythonpath.
#
# The pre-baked runtime venv location is overridable via MAC_CONTRACT_RUNTIME_VENV
# (default /opt/mac-venv). This keeps production behavior byte-identical while
# letting the runner's own contract tests stay hermetic on hosts that DO ship a
# real /opt/mac-venv: a test can point the override at a nonexistent path so the
# fake python3 it places on PATH is the one that gets resolved, instead of the
# host's real interpreter silently winning and running the whole suite.
# The default is the pre-baked runtime interpreter /opt/mac-venv/bin/python; an
# override supplies the venv ROOT (its bin/python is appended below).
if [ -n "$_MAC_CONTRACT_RUNTIME_VENV_REQUESTED" ]; then
    _MAC_CONTRACT_RUNTIME_PYTHON="$_MAC_CONTRACT_RUNTIME_VENV_REQUESTED/bin/python"
else
    _MAC_CONTRACT_RUNTIME_PYTHON="/opt/mac-venv/bin/python"
fi
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
elif [ -x "$_MAC_CONTRACT_RUNTIME_PYTHON" ]; then
    PY="$_MAC_CONTRACT_RUNTIME_PYTHON"
else
    PY="$(command -v python3 || command -v python || true)"
fi
if [ -z "${PY}" ]; then
    echo "run-contract-tests.sh: no python interpreter found (.venv, $_MAC_CONTRACT_RUNTIME_PYTHON, or PATH)" >&2
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
    # A pre-existing but broken .venv (stale deps, a half-written interpreter,
    # or one built against removed system libs) is resolved as $PY above yet
    # still fails the probe. bootstrap-project.py only rebuilds a venv whose
    # bin/python is MISSING, so it would leave this one untouched and the gate
    # would exit 1 without ever repairing it — the same environment-prerequisite
    # dead end the on-demand bootstrap exists to prevent. Discard the unusable
    # .venv first so bootstrap builds a clean one.
    if [ -x ".venv/bin/python" ] && ! _py_can_run_suite ".venv/bin/python"; then
        rm -rf .venv
        # $PY may have resolved to the .venv interpreter just removed; fall back
        # to the runtime venv or a PATH python so bootstrap has a real builder.
        if [ ! -x "$PY" ]; then
            if [ -x "$_MAC_CONTRACT_RUNTIME_PYTHON" ]; then
                PY="$_MAC_CONTRACT_RUNTIME_PYTHON"
            else
                PY="$(command -v python3 || command -v python || true)"
            fi
        fi
    fi
    if [ ! -x ".venv/bin/python" ] && [ -n "$PY" ] && [ -x "$PY" ] \
        && [ -f "scripts/bootstrap-project.py" ]; then
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

# Fail the cheapest repository-wide consistency contracts before paying for
# xdist startup, subprocess/container tests, and full coverage. These checks
# are intentionally existing tests rather than a second implementation of the
# rules: generated config/reference drift, invalid executable documentation,
# and a published Python API break now fail in seconds while the complete suite
# remains the final authority. Nested invocations are runner contract tests
# themselves, so they skip this outer preflight and retain a single pytest
# owner.
if [ "$#" -eq 0 ] && [ "$_MAC_TEST_NESTED_PYTEST" = "0" ]; then
    echo "run-contract-tests.sh: running fail-fast repository contract preflight"
    "$PY" scripts/test-docs.py --static-only
    "$PY" scripts/generate-env-config-registry.py --check
    "$PY" scripts/generate-docs-reference.py --check
    # Repository-wide consistency contracts, run as ONE pytest invocation so
    # the whole set costs a single interpreter start (~2s together; the
    # slowest is 0.4s).
    #
    # These belong here for the same reason as the generated-artifact checks
    # above: they are properties of the repository rather than of the change,
    # they are near-instant, and they were previously reported only after the
    # complete suite had run. On 2026-08-20 that cost three sequential ~45
    # minute CI round trips to learn three things detectable in under a second
    # -- an agent name left in a checked-in ADR, a CLI subcommand added without
    # a tests/cli/ test, and a stale generated registry.
    #
    # Measured on this repository: failing CI runs average 47 minutes against
    # 11 for passing ones, because a failure at the END of the suite still pays
    # for the whole suite. Moving the cheap invariants to the front is the
    # single largest available reduction in iteration time.
    #
    # -x so the first failure stops the preflight: everything here is a
    # separate contract, and reporting six at once is no more actionable than
    # reporting the first.
    "$PY" -m pytest -q -x \
        tests/test_control_plane_public_contract.py \
        tests/test_docs_no_operator_identity.py \
        tests/test_docs_accessibility.py \
        tests/test_docs_reference_interpreter_independence.py \
        tests/test_generated_artifact_guards_always_run.py \
        tests/test_repository_hygiene.py \
        tests/test_no_dead_indexes.py \
        tests/cli/test_cli_coverage_gate.py \
        tests/cli/test_cli_human_interface_coverage.py
fi

# --------------------------------------------------------------------------
# Checkpointing helpers (see src/mac/test_checkpoint.py for the rules).
#
# CORRECTNESS BEATS SPEED. Every one of these fails OPEN: a missing, stale,
# unreadable, or ambiguous checkpoint leaves MAC_TEST_CHECKPOINT_SKIP_FILE
# unset, the conftest deselection hook inert, and the complete selection
# running. A corrupt checkpoint can never make a red suite look green.
# --------------------------------------------------------------------------
_MAC_CHECKPOINT_DIR=""
_MAC_CHECKPOINT_SKIP_FILE=""
_MAC_CHECKPOINT_RESUMED=0

_mac_checkpoint_enabled() {
    [ "$_MAC_TEST_CHECKPOINT_REQUESTED" = "1" ] && [ "$_MAC_TEST_NESTED_PYTEST" = "0" ]
}

# Prepare a clean recording namespace for the pytest invocation(s) that follow.
# Also clears any carry-forward from a previous phase, so a phase that does not
# explicitly plan a resume runs everything.
_mac_checkpoint_arm() {
    _mac_checkpoint_enabled || return 0
    if [ -z "$_MAC_CHECKPOINT_DIR" ]; then
        if [ -n "$_MAC_TEST_CHECKPOINT_DIR_REQUESTED" ]; then
            _MAC_CHECKPOINT_DIR="$_MAC_TEST_CHECKPOINT_DIR_REQUESTED"
        else
            _MAC_CHECKPOINT_DIR="$(pwd)/.mac-test-checkpoint"
        fi
        _MAC_CHECKPOINT_SKIP_FILE="$_MAC_CHECKPOINT_DIR/carried-forward.txt"
    fi
    mkdir -p "$_MAC_CHECKPOINT_DIR"
    rm -rf "$_MAC_CHECKPOINT_DIR/results"
    export MAC_TEST_CHECKPOINT_RESULTS_DIR="$_MAC_CHECKPOINT_DIR/results"
    # Which pytest session OWNS the recording namespace. The suite has tests
    # that shell out to pytest and they inherit the variables above; without
    # this, a fixture project's deliberately-failing test lands in the real
    # repository's checkpoint (observed on this feature's first smoke run). The
    # conftest hooks record only when their rootdir is this one AND they were
    # not spawned from inside a running test.
    export MAC_TEST_CHECKPOINT_ROOT="$(pwd)"
    unset MAC_TEST_CHECKPOINT_SKIP_FILE
    _MAC_CHECKPOINT_RESUMED=0
}

# $1 = 1 when this gate enforces whole-repo coverage floors (so a resume is only
# a triage pass), 0 when it measures no coverage or only changed-line coverage.
_mac_checkpoint_plan() {
    _mac_checkpoint_enabled || return 0
    [ -n "$_MAC_CHECKPOINT_DIR" ] || return 0
    rm -f "$_MAC_CHECKPOINT_SKIP_FILE"
    _ckpt_require=""
    if [ "$1" = "1" ]; then
        _ckpt_require="--require-whole-coverage"
    fi
    if "$PY" scripts/test-checkpoint.py --dir "$_MAC_CHECKPOINT_DIR" plan \
        $_ckpt_require --skip-file "$_MAC_CHECKPOINT_SKIP_FILE"; then
        export MAC_TEST_CHECKPOINT_SKIP_FILE="$_MAC_CHECKPOINT_SKIP_FILE"
        _MAC_CHECKPOINT_RESUMED=1
    else
        _MAC_CHECKPOINT_RESUMED=0
    fi
}

# $1 = gate label recorded in the checkpoint document.
_mac_checkpoint_record() {
    _mac_checkpoint_enabled || return 0
    [ -n "$_MAC_CHECKPOINT_DIR" ] || return 0
    _ckpt_carry=""
    if [ "$_MAC_CHECKPOINT_RESUMED" = "1" ]; then
        _ckpt_carry="--carried-forward-file $_MAC_CHECKPOINT_SKIP_FILE"
    fi
    "$PY" scripts/test-checkpoint.py --dir "$_MAC_CHECKPOINT_DIR" record \
        --gate "$1" $_ckpt_carry || true
}

# A resumed run can legitimately deselect EVERY collected test (nothing in the
# selection changed since the checkpoint), which pytest reports as exit 5, "no
# tests ran". That is the success case, not a misconfiguration: the results being
# carried forward are exactly the evidence exit 5 says is missing. Remapped ONLY
# when this run actually resumed, so a genuinely empty selection on an
# unresumed run stays the hard error it has always been.
_mac_checkpoint_exit() {
    # $1 = observed pytest exit status; echoes the effective status.
    if [ "$1" -eq 5 ] && [ "$_MAC_CHECKPOINT_RESUMED" = "1" ]; then
        echo "run-contract-tests.sh: every selected test was carried forward from the checkpoint; nothing left to run" >&2
        echo 0
        return 0
    fi
    echo "$1"
}

# pytest exit code 5 means "no tests were collected". When this runner is
# invoked from INSIDE a pytest/xdist worker (the outer merge gate parallelizes
# the whole suite, and some contract tests shell out to this very script), the
# child's outer selection can legitimately match zero tests in this process even
# though the same tests pass when run directly. Treating that empty selection as
# a hard gate failure is the exit-5 non-code failure this task removes. Remap
# ONLY exit 5, and ONLY when nested, to success — every other status (1 failed,
# 2 interrupted/usage, 3 internal error, 4 usage error) still propagates so real
# test/lint/collection failures are never masked. Outside a nested context an
# empty selection stays a hard error, because a top-level whole-suite run that
# collects nothing is a genuine misconfiguration.
_mac_pytest_exit() {
    # $1 = observed pytest exit status; echoes the effective status.
    _status="$1"
    if [ "$_status" -eq 5 ] && [ "$_MAC_TEST_NESTED_PYTEST" = "1" ]; then
        echo "run-contract-tests.sh: nested pytest collected 0 items (exit 5); treating the empty outer selection as a non-failure" >&2
        echo 0
        return 0
    fi
    echo "$_status"
}

if [ "$#" -eq 0 ]; then
    portfolio_dir=""
    if [ "$_MAC_TEST_PORTFOLIO_REQUESTED" = "1" ]; then
        portfolio_dir="$(pwd)/.test-portfolio"
        rm -rf "$portfolio_dir"
        mkdir -p "$portfolio_dir"
        export MAC_TEST_PORTFOLIO_OUTPUT="$portfolio_dir/timings.json"
    fi
    # MAC_TEST_JOBS controls worker count: unset => auto (one per core); an
    # explicit integer pins the worker count; "0" => disable xdist entirely
    # (serial), the fallback for memory- or core-constrained hosts. The value is
    # captured before the hermetic MAC_* environment sweep above. Portfolio mode
    # always runs serial so per-test timing/coverage attribution stays exact.
    _MAC_TEST_JOBS="$_MAC_TEST_JOBS_REQUESTED"
    case "$_MAC_TEST_JOBS" in
        '')
            # Unset: headroom-aware default (~2/3 of cores, min 2) so real
            # subprocess/container work does not oversubscribe the host. On the
            # co-located hub host (control plane + worker) the hub load-shed cap
            # narrows this to a fraction of cores so one gate run can never
            # consume the whole box and starve the control plane; non-hub hosts
            # print nothing and fall through to the default. (task_1bd5db4b)
            _MAC_HUB_JOBS="$("$PY" - <<'PYCAP'
import os, sys
try:
    from mac.hub_load_shed import resolve_hub_test_jobs
    jobs = resolve_hub_test_jobs(
        os.environ.get("MAC_AGENT_ID", ""),
        os.environ.get("MAC_AGENT_NAME", ""),
    )
except Exception:
    jobs = None
if jobs:
    sys.stdout.write(str(jobs))
PYCAP
)"
            if [ -n "$_MAC_HUB_JOBS" ]; then
                _MAC_TEST_JOBS="$_MAC_HUB_JOBS"
                echo "run-contract-tests.sh: hub host load-shed cap: MAC_TEST_JOBS=$_MAC_TEST_JOBS" >&2
            else
                _MAC_TEST_JOBS="$("$PY" -c 'import os; print(max(2, (os.cpu_count() or 2) * 2 // 3))')"
            fi
            ;;
        0|auto) ;;
        *[!0-9]*)
            echo "run-contract-tests.sh: MAC_TEST_JOBS must be 0, auto, or a positive integer" >&2
            exit 2
            ;;
        *)
            if [ "$_MAC_TEST_JOBS" -lt 1 ]; then
                echo "run-contract-tests.sh: MAC_TEST_JOBS must be 0, auto, or a positive integer" >&2
                exit 2
            fi
            ;;
    esac
    # Nested inside a pytest/xdist worker: force a SINGLE serial owner. Setting
    # jobs to 0 guarantees the two-controller/zero-collection fan-out can never
    # start — both the coverage and fast paths below then take their single
    # serial "$PY -m pytest" branch instead of spawning an xdist pool.
    if [ "$_MAC_TEST_NESTED_PYTEST" = "1" ] && [ "$_MAC_TEST_JOBS" != "0" ]; then
        echo "run-contract-tests.sh: nested pytest detected; forcing a single" \
             "serial owner (no nested xdist controller / no zero-collection pool)" >&2
        _MAC_TEST_JOBS=0
    fi
    # Tests marked process_e2e/postgres/container bind real ports and spawn real
    # processes, so concurrent scheduling would collide. They run SERIALLY in a
    # second invocation; the xdist-safe bulk slice runs first under --dist
    # loadscope (which keeps a module's tests + module-scoped fixtures on one
    # worker).
    _SERIAL_MARK="process_e2e or postgres or container_contract or docker_e2e"
    # Pin the protected marker slice to one process. PYTEST_ADDOPTS is unset
    # above, but an explicit `-n 0` is what keeps a future pyproject addopts
    # (or a leaked `-n auto`) from making process_e2e concurrent after we split
    # it off. The plugin drops coverage's subprocess patch for children:
    # tracing docker/sudo/launchctl blows sub-second process-group deadlines
    # on Darwin after a wide xdist bulk phase. The parent pytest process is
    # still measured by `coverage run`.
    _mac_run_serial_pytest() {
        export MAC_TEST_SERIAL_SLICE=1
        _serial_status=0
        _serial_pypath="$(cd "$(dirname "$0")" && pwd)${PYTHONPATH:+:$PYTHONPATH}"
        if [ "${1:-}" = "coverage" ]; then
            PYTHONPATH="$_serial_pypath" "$PY" -m coverage run -m pytest -n 0 \
                -p serial_slice_plugin -m "$_SERIAL_MARK" || _serial_status=$?
        else
            PYTHONPATH="$_serial_pypath" "$PY" -m pytest -n 0 \
                -p serial_slice_plugin -m "$_SERIAL_MARK" || _serial_status=$?
        fi
        unset MAC_TEST_SERIAL_SLICE
        return "$_serial_status"
    }

    # Every invocation owns a separate coverage namespace. Multiple agents and
    # local actors routinely test the same checkout concurrently; sharing the
    # default .coverage* and coverage.json files let one run erase, combine, or
    # truncate another run between JSON generation and policy evaluation.
    # Evaluate only the invocation-private report, then atomically publish a
    # conventional coverage.json artifact after the policy has consumed it.
    coverage_json=""
    if [ "$_MAC_TEST_COVERAGE_REQUESTED" != "0" ]; then
        _MAC_COVERAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mac-contract-coverage.XXXXXX")"
        chmod 0700 "$_MAC_COVERAGE_DIR"
        export COVERAGE_FILE="$_MAC_COVERAGE_DIR/.coverage"
        coverage_json="$_MAC_COVERAGE_DIR/coverage.json"
    fi

    # Impact-based subset gate. Placed before the coverage machinery so a focused
    # resolution runs the small selected set SERIALLY (subsets are small by
    # design; serial avoids the real-port/subprocess collisions the marker split
    # exists to prevent) and enforces diff-coverage. 'full'/error falls through.
    if [ -n "$_MAC_TEST_SELECT_BASE_REQUESTED" ]; then
        selection_json="$(mktemp "${TMPDIR:-/tmp}/mac-impact-select.XXXXXX")"
        resolve_status=0
        "$PY" scripts/resolve-impacted-tests.py \
            --base "$_MAC_TEST_SELECT_BASE_REQUESTED" >"$selection_json" 2>/dev/null \
            || resolve_status=$?
        sel_mode=""
        if [ "$resolve_status" -eq 0 ]; then
            sel_mode="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["mode"])' "$selection_json" 2>/dev/null || echo "")"
        fi
        if [ "$sel_mode" = "focused" ]; then
            "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); print("impact selection: %s (%s), %d tests" % (d["mode"], d["reason"], len(d.get("tests", []))))' "$selection_json"
            set --
            while IFS= read -r _t; do
                [ -n "$_t" ] && set -- "$@" "$_t"
            done < <("$PY" -c 'import json,sys; [print(x) for x in json.load(open(sys.argv[1])).get("tests", [])]' "$selection_json")
            rm -f "$selection_json"
            if [ "$#" -eq 0 ]; then
                echo "impact selection: no tests exercise the changed code (non-code change)"
                exit 0
            fi
            # Checkpointing is safe on this path even with coverage on: the gate
            # here enforces CHANGED-LINE coverage, and the carry-forward rule
            # re-runs every test the impact map attributes to a changed source
            # file, so the changed lines are measured exactly as they would be
            # without a resume. Whole-repo floors are not evaluated here at all.
            _mac_checkpoint_arm
            _mac_checkpoint_plan 0
            if [ "$_MAC_TEST_COVERAGE_REQUESTED" = "0" ]; then
                sel_fast_status=0
                "$PY" -m pytest "$@" || sel_fast_status=$?
                _mac_checkpoint_record "focused-fast"
                exit "$(_mac_checkpoint_exit "$sel_fast_status")"
            fi
            "$PY" -m coverage erase
            sel_status=0
            "$PY" -m coverage run -m pytest "$@" || sel_status=$?
            if [ "$sel_status" -eq 5 ] && [ "$_MAC_CHECKPOINT_RESUMED" = "1" ]; then
                # Every selected test was carried forward, so nothing ran and
                # there is no coverage data -- and this path still has to prove
                # the CHANGED LINES are covered. Measuring that from an empty
                # data set would report every changed line uncovered and fail a
                # gate that should pass. Drop the carry-forward and run the
                # focused selection in full; correctness beats speed.
                echo "impact selection: every selected test was carried forward; re-running the focused selection in full so changed-line coverage is measurable"
                unset MAC_TEST_CHECKPOINT_SKIP_FILE
                _MAC_CHECKPOINT_RESUMED=0
                sel_status=0
                "$PY" -m coverage run -m pytest "$@" || sel_status=$?
            fi
            _mac_checkpoint_record "focused"
            "$PY" -m coverage combine >/dev/null 2>&1 || true
            diff_status=0
            if [ "$sel_status" -eq 0 ]; then
                "$PY" -m coverage json -o "$coverage_json" || diff_status=$?
                if [ "$diff_status" -eq 0 ]; then
                    "$PY" scripts/coverage-policy.py --coverage-json "$coverage_json" \
                        --mode diff --base "$_MAC_TEST_SELECT_BASE_REQUESTED" \
                        --repo-root "$(pwd)" || diff_status=$?
                fi
            fi
            if [ -s "$coverage_json" ]; then
                coverage_publish="coverage.json.tmp.$$"
                cp -f "$coverage_json" "$coverage_publish"
                mv -f "$coverage_publish" coverage.json
            fi
            if [ "$sel_status" -ne 0 ]; then
                exit "$sel_status"
            fi
            exit "$diff_status"
        fi
        # 'full' or resolver error: fall through to the whole-repo gate below.
        rm -f "$selection_json"
        echo "impact selection: full run required (resolver mode=${sel_mode:-error}); running whole-repo gate"
    fi

    # Fast verification path: identical bulk+serial split, but WITHOUT coverage
    # tracing or the floor policy. Coverage roughly halves throughput and adds
    # per-worker memory that a pass/fail rollout/dev check does not need. Never
    # taken in portfolio mode (which requires coverage) or for the merge gate
    # (which leaves MAC_TEST_COVERAGE at its default of 1).
    if [ "$_MAC_TEST_COVERAGE_REQUESTED" = "0" ] && [ "$_MAC_TEST_PORTFOLIO_REQUESTED" != "1" ]; then
        # Re-export the namespace switch stripped by the hermetic MAC_* sweep so
        # the conftest deselection hook sees it in the pytest child. Only reached
        # in fast mode; the guard above blocks it whenever coverage is on.
        if [ -n "$_MAC_TEST_DISABLE_GROUPS_REQUESTED" ]; then
            export MAC_TEST_DISABLE_GROUPS="$_MAC_TEST_DISABLE_GROUPS_REQUESTED"
        fi
        # No coverage is measured on this path, so a green resumed run is
        # terminal: there is no coverage number for a subset to distort.
        _mac_checkpoint_arm
        _mac_checkpoint_plan 0
        pytest_status=0
        if [ "$_MAC_TEST_JOBS" != "0" ]; then
            "$PY" -m pytest -n "$_MAC_TEST_JOBS" --dist loadscope \
                -m "not ($_SERIAL_MARK)" || pytest_status=$?
            if [ "$pytest_status" -eq 0 ]; then
                _mac_run_serial_pytest || pytest_status=$?
            fi
        else
            # Nested/serial single-owner run. A nested empty selection (exit 5)
            # is remapped to success by _mac_pytest_exit; real failures survive.
            "$PY" -m pytest || pytest_status=$?
            pytest_status="$(_mac_pytest_exit "$pytest_status")"
        fi
        _mac_checkpoint_record "fast"
        exit "$(_mac_checkpoint_exit "$pytest_status")"
    fi

    # CHECKPOINT TRIAGE PASS.
    #
    # This is the coverage-enforcing gate: statement/branch floors are computed
    # over the WHOLE repository, and both currently sit within 0.35pp of the
    # floor. A subset cannot produce that number, and combining a previous run's
    # coverage data with a resumed run's would OVER-state coverage for any file
    # whose executing tests changed -- turning a red gate green, which is the
    # exact failure this whole feature must never cause. So coverage is never
    # resumed. What is resumed is FAILURE DETECTION: run the not-carried-forward
    # tests first, without coverage. Red in minutes instead of an hour; green
    # falls straight through to the complete, unresumed, coverage-measured gate
    # below, byte-identical to what it has always been.
    _mac_checkpoint_arm
    _mac_checkpoint_plan 1
    if [ "$_MAC_CHECKPOINT_RESUMED" = "1" ]; then
        echo "run-contract-tests.sh: checkpoint triage pass (no coverage; the" \
             "whole-repo floors are enforced by the complete gate that follows a green triage)"
        triage_status=0
        if [ "$_MAC_TEST_PORTFOLIO_REQUESTED" != "1" ] && [ "$_MAC_TEST_JOBS" != "0" ]; then
            "$PY" -m pytest -n "$_MAC_TEST_JOBS" --dist loadscope \
                -m "not ($_SERIAL_MARK)" || triage_status=$?
            if [ "$triage_status" -eq 0 ]; then
                _mac_run_serial_pytest || triage_status=$?
            fi
        else
            "$PY" -m pytest || triage_status=$?
        fi
        # Exit 5 here means every remaining test was carried forward, i.e. the
        # triage had nothing to check. That is not a failure; the complete gate
        # below is still the authority.
        if [ "$triage_status" -eq 5 ]; then
            triage_status=0
        fi
        _mac_checkpoint_record "triage"
        if [ "$triage_status" -ne 0 ]; then
            echo "run-contract-tests.sh: checkpoint triage pass FAILED; the complete" \
                 "coverage gate was not attempted. Fix the failures above and re-run —" \
                 "the next run resumes from this checkpoint." >&2
            exit "$triage_status"
        fi
        echo "run-contract-tests.sh: checkpoint triage pass green; running the" \
             "complete coverage-measured gate (whole-repo floors need every test)"
    fi
    # The complete gate runs everything, always: clear any carry-forward so the
    # deselection hook is inert, and start a fresh recording namespace.
    _mac_checkpoint_arm

    "$PY" -m coverage erase
    # `patch = ["subprocess"]` in pyproject.toml makes the parent and every
    # Python child (including pytest-xdist workers) write parallel coverage
    # data, so a single `coverage combine` merges every process's data set —
    # this is what lets the run below split into two pytest invocations while
    # still enforcing one coverage total.
    pytest_status=0
    if [ "$_MAC_TEST_PORTFOLIO_REQUESTED" != "1" ] && [ "$_MAC_TEST_JOBS" != "0" ]; then
        "$PY" -m coverage run -m pytest -n "$_MAC_TEST_JOBS" --dist loadscope \
            -m "not ($_SERIAL_MARK)" || pytest_status=$?
        if [ "$pytest_status" -eq 0 ]; then
            _mac_run_serial_pytest coverage || pytest_status=$?
        fi
    else
        # Nested/serial single-owner run under coverage. A nested empty
        # selection (exit 5) is remapped to success; real failures survive.
        "$PY" -m coverage run -m pytest || pytest_status=$?
        pytest_status="$(_mac_pytest_exit "$pytest_status")"
    fi
    # Record the complete run's outcomes. This is the checkpoint a later failed
    # iteration resumes from, and it is always written from a full suite, so it
    # never inherits a subset's blind spots.
    _mac_checkpoint_record "full"
    # coverage.py can report a corrupt parallel data file as "1 file errored"
    # while still exiting zero after combining the remaining files.  That is a
    # partial measurement, not a passing gate.  Preserve the combine output and
    # promote either its nonzero status or its partial-combine diagnostic to a
    # hard failure before generating or evaluating coverage.json.
    combine_log="$(mktemp "${TMPDIR:-/tmp}/mac-coverage-combine.XXXXXX")"
    combine_status=0
    "$PY" -m coverage combine 2>&1 | tee "$combine_log" || combine_status=$?
    if grep -Eiq "couldn't combine data file|[1-9][0-9]* files? errored" "$combine_log"; then
        combine_status=1
    fi
    rm -f "$combine_log"

    json_status=0
    report_status=0
    policy_status=0
    if [ "$combine_status" -eq 0 ]; then
        "$PY" -m coverage json -o "$coverage_json" || json_status=$?
    fi
    if [ "$combine_status" -eq 0 ] && [ "$json_status" -eq 0 ]; then
        "$PY" -m coverage report || report_status=$?
        coverage_policy_args=""
        if [ "$pytest_status" -ne 0 ]; then
            coverage_policy_args="--partial"
        fi
        # A failed pytest phase leaves an incomplete measurement (and can
        # suppress the serial phase). Label it as partial; pytest_status below
        # remains the gate result rather than a misleading coverage failure.
        "$PY" scripts/coverage-policy.py --coverage-json "$coverage_json" \
            $coverage_policy_args || policy_status=$?
    fi
    if [ -s "$coverage_json" ]; then
        coverage_publish="coverage.json.tmp.$$"
        cp -f "$coverage_json" "$coverage_publish"
        mv -f "$coverage_publish" coverage.json
    fi
    portfolio_status=0
    if [ -n "$portfolio_dir" ]; then
        portfolio_coverage_tmp="$portfolio_dir/.coverage.tmp.$$"
        cp -f "$COVERAGE_FILE" "$portfolio_coverage_tmp"
        mv -f "$portfolio_coverage_tmp" "$portfolio_dir/.coverage"
        "$PY" scripts/test-portfolio.py --output-dir "$portfolio_dir" --report-only \
            || portfolio_status=$?
    fi
    if [ "$pytest_status" -ne 0 ]; then
        exit "$pytest_status"
    fi
    if [ "$combine_status" -ne 0 ]; then
        exit "$combine_status"
    fi
    if [ "$json_status" -ne 0 ]; then
        exit "$json_status"
    fi
    if [ "$report_status" -ne 0 ]; then
        exit "$report_status"
    fi
    if [ "$portfolio_status" -ne 0 ]; then
        exit "$portfolio_status"
    fi
    # Scheduled full run: rebuild the committed impact map from this run's fresh
    # per-test coverage. Only on a green portfolio gate (policy passed) with the
    # portfolio artifacts present; committing the refreshed map is the caller's
    # (CI) job, keeping this script side-effect-free on the working tree apart
    # from the tracked artifact it is explicitly asked to regenerate.
    if [ "$_MAC_TEST_REBUILD_MAP_REQUESTED" = "1" ] && [ -n "$portfolio_dir" ] \
        && [ "$policy_status" -eq 0 ]; then
        if ! "$PY" scripts/build-test-impact-map.py \
            --coverage-file "$COVERAGE_FILE" \
            --timings "$portfolio_dir/timings.json" \
            --output "src/mac/data/test_impact_map.json"
        then
            echo "run-contract-tests.sh: impact-map rebuild failed" >&2
            exit 1
        fi
    fi
    exit "$policy_status"
fi

# Explicit test targets (arguments forwarded here) can also be dispatched from
# inside a nested pytest/xdist worker. Run them with a single serial owner and
# apply the same nested exit-5 remap so a legitimately empty child selection is
# not misread as a hard failure, while every real failure still propagates.
#
# This path measures no coverage at all, so a green resumed run is terminal:
# there is no coverage number a subset could distort. It is the route the PR
# `sanity` job takes for a focused selection (run-sanity-tests.sh execs this
# script with the selected test paths).
_mac_checkpoint_arm
_mac_checkpoint_plan 0
_mac_argv_status=0
"$PY" -m pytest "$@" || _mac_argv_status=$?
_mac_checkpoint_record "argv"
_mac_argv_status="$(_mac_checkpoint_exit "$_mac_argv_status")"
exit "$(_mac_pytest_exit "$_mac_argv_status")"
