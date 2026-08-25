#!/usr/bin/env bash
# Shared lint gate for the mac control plane.
#
# One Ruff configuration (see [tool.ruff] in pyproject.toml) is the single
# source of truth, so every developer, pre-push hook, and CI host runs
# byte-identical rules instead of ad-hoc per-directory settings.
#
#   scripts/run-lint.sh                # diagnose: ruff check + ruff format --check
#   scripts/run-lint.sh --fix          # apply:    ruff check --fix + ruff format
#   scripts/run-lint.sh --format-check # format only (same checker lint already runs)
#
# `make lint` and `make lint-fix` are a diagnose/apply pair. Both cover the
# same two tools. Formatting is part of the gate so lint-fix cannot rewrite
# hundreds of files that lint never mentioned.
#
# Ruff is a dev-only tool fetched on demand with `uv run --with ruff`, matching
# how scripts/dead-code-check.sh runs vulture; it is intentionally NOT a runtime
# dependency of the shipped wheel.
set -euo pipefail
cd "$(dirname "$0")/.."

ruff() { uv run --with ruff ruff "$@"; }

case "${1:-}" in
    --fix)
        ruff check --fix .
        ruff format .
        echo "run-lint: applied lint autofixes and formatting"
        ;;
    --format-check)
        ruff format --check .
        ;;
    "")
        check_rc=0
        format_rc=0
        ruff check . || check_rc=$?
        ruff format --check . || format_rc=$?
        if [ "$check_rc" -ne 0 ] || [ "$format_rc" -ne 0 ]; then
            echo "run-lint: check_rc=$check_rc format_rc=$format_rc" >&2
            exit 1
        fi
        echo "run-lint: clean (lint and format)"
        ;;
    *)
        echo "run-lint: unknown option '$1' (use --fix or --format-check)" >&2
        exit 2
        ;;
esac
