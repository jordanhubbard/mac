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
# Pytest configuration belongs to this repository and the explicit arguments
# passed to this runner.  In particular, an inherited ``-n auto`` must not make
# the protected process/container phase concurrent after the runner separates
# it from the xdist-safe bulk phase.
unset PYTEST_ADDOPTS

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
    # MAC_TEST_JOBS controls worker count: unset => auto (one per core); an
    # explicit integer pins the worker count; "0" => disable xdist entirely
    # (serial), the fallback for memory- or core-constrained hosts. The value is
    # captured before the hermetic MAC_* environment sweep above. Portfolio mode
    # always runs serial so per-test timing/coverage attribution stays exact.
    _MAC_TEST_JOBS="$_MAC_TEST_JOBS_REQUESTED"
    case "$_MAC_TEST_JOBS" in
        '')
            # Unset: headroom-aware default (~2/3 of cores, min 2) so real
            # subprocess/container work does not oversubscribe the host.
            _MAC_TEST_JOBS="$("$PY" -c 'import os; print(max(2, (os.cpu_count() or 2) * 2 // 3))')"
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
    # Tests marked process_e2e/postgres/container bind real ports and spawn real
    # processes, so concurrent scheduling would collide. They run SERIALLY in a
    # second invocation; the xdist-safe bulk slice runs first under --dist
    # loadscope (which keeps a module's tests + module-scoped fixtures on one
    # worker).
    _SERIAL_MARK="process_e2e or postgres or container_contract or docker_e2e"

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
            if [ "$_MAC_TEST_COVERAGE_REQUESTED" = "0" ]; then
                exec "$PY" -m pytest "$@"
            fi
            "$PY" -m coverage erase
            sel_status=0
            "$PY" -m coverage run -m pytest "$@" || sel_status=$?
            "$PY" -m coverage combine >/dev/null 2>&1 || true
            diff_status=0
            if [ "$sel_status" -eq 0 ]; then
                "$PY" -m coverage json -o coverage.json || diff_status=$?
                if [ "$diff_status" -eq 0 ]; then
                    "$PY" scripts/coverage-policy.py --coverage-json coverage.json \
                        --mode diff --base "$_MAC_TEST_SELECT_BASE_REQUESTED" \
                        --repo-root "$(pwd)" || diff_status=$?
                fi
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
        pytest_status=0
        if [ "$_MAC_TEST_JOBS" != "0" ]; then
            "$PY" -m pytest -n "$_MAC_TEST_JOBS" --dist loadscope \
                -m "not ($_SERIAL_MARK)" || pytest_status=$?
            if [ "$pytest_status" -eq 0 ]; then
                "$PY" -m pytest -m "$_SERIAL_MARK" || pytest_status=$?
            fi
        else
            "$PY" -m pytest || pytest_status=$?
        fi
        exit "$pytest_status"
    fi

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
            "$PY" -m coverage run -m pytest -m "$_SERIAL_MARK" || pytest_status=$?
        fi
    else
        "$PY" -m coverage run -m pytest || pytest_status=$?
    fi
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
        "$PY" -m coverage json -o coverage.json || json_status=$?
    fi
    if [ "$combine_status" -eq 0 ] && [ "$json_status" -eq 0 ]; then
        "$PY" -m coverage report || report_status=$?
        "$PY" scripts/coverage-policy.py --coverage-json coverage.json || policy_status=$?
    fi
    portfolio_status=0
    if [ -n "$portfolio_dir" ]; then
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
        "$PY" scripts/build-test-impact-map.py \
            --coverage-file "$portfolio_dir/.coverage" \
            --timings "$portfolio_dir/timings.json" \
            --output "src/mac/data/test_impact_map.json" \
            || echo "run-contract-tests.sh: impact-map rebuild failed (gate result unaffected)" >&2
    fi
    exit "$policy_status"
fi

"$PY" -m pytest "$@"
