#!/usr/bin/env bash
# Shared lint gate for the mac control plane.
#
# One Ruff configuration (see [tool.ruff] in pyproject.toml) is the single
# source of truth, so every developer, pre-push hook, and CI host runs
# byte-identical rules instead of ad-hoc per-directory settings.
#
#   scripts/run-lint.sh                # check-only: the enforceable lint gate
#   scripts/run-lint.sh --fix          # apply safe lint autofixes AND reformat
#   scripts/run-lint.sh --format-check # preview formatting drift (advisory)
#
# The default check enforces only the always-green correctness floor defined by
# [tool.ruff.lint].select, so it is red only for a real regression. Formatting
# is NOT part of the default gate yet: the historical tree is not fully
# ruff-formatted, so a repo-wide `ruff format --check` would be noise. Use
# `--fix` to normalize the files you touch, and widen the select list as the
# codebase is cleaned up.
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
        echo "run-lint: applied safe autofixes and formatting"
        ;;
    --format-check)
        ruff format --check .
        ;;
    "")
        ruff check .
        echo "run-lint: clean (lint select set satisfied)"
        ;;
    *)
        echo "run-lint: unknown option '$1' (use --fix or --format-check)" >&2
        exit 2
        ;;
esac
