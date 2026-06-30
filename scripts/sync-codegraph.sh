#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CODEGRAPH_BIN=${MAC_CODEGRAPH_BIN:-codegraph}

if ! command -v "$CODEGRAPH_BIN" >/dev/null 2>&1; then
    echo "CodeGraph is required but '$CODEGRAPH_BIN' was not found on PATH" >&2
    echo "Install it with: curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh" >&2
    exit 127
fi

cd "$ROOT"
if [ -d .codegraph ]; then
    "$CODEGRAPH_BIN" sync --quiet .
else
    "$CODEGRAPH_BIN" init .
fi
