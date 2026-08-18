#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CODEGRAPH_BIN=${MAC_CODEGRAPH_BIN:-codegraph}

# CodeGraph is a developer/agent code-intelligence index. It is NOT required to
# build, install, test or deploy mac, and this script used to exit 127 when it
# was missing -- which made `make install` fail on any machine without it, since
# codegraph-sync is a prerequisite of install, build, test, coverage, setup and
# deploy alike. Observed on puck.local:
#
#     CodeGraph is required but 'codegraph' was not found on PATH
#     make: *** [codegraph-sync] Error 127
#
# Everything downstream already tolerates its absence. scripts/resolve-impacted-
# tests.py treats an unavailable CodeGraph as a reason to FAIL CLOSED to a full
# test run -- CI has done exactly that ("sanity selection: full
# (codegraph_unavailable)") -- and the repository ships no committed index, so a
# fresh clone never has one.
#
# Set MAC_REQUIRE_CODEGRAPH=1 to restore the hard failure, for a machine where
# the index is meant to exist and its absence should stop the build.
if ! command -v "$CODEGRAPH_BIN" >/dev/null 2>&1; then
    if [ "${MAC_REQUIRE_CODEGRAPH:-0}" = "1" ]; then
        echo "CodeGraph is required (MAC_REQUIRE_CODEGRAPH=1) but '$CODEGRAPH_BIN' was not found on PATH" >&2
        echo "Install it with: curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh" >&2
        exit 127
    fi
    echo "codegraph-sync: '$CODEGRAPH_BIN' not on PATH; skipping the index." >&2
    echo "  mac does not need it to build, install, test or deploy. Test" >&2
    echo "  selection falls back to a full run without it." >&2
    echo "  To index anyway: curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh" >&2
    exit 0
fi

cd "$ROOT"
if [ -d .codegraph ]; then
    "$CODEGRAPH_BIN" sync --quiet .
else
    "$CODEGRAPH_BIN" init .
fi
