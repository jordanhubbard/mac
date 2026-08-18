#!/bin/sh
set -eu

# Install CodeGraph if it is not already present.
#
# WHY THIS IS PART OF `make install` RATHER THAN A HARD REQUIREMENT.
# CodeGraph is not needed to BUILD mac -- no index is committed, indexes are
# generated per-directory, and scripts/resolve-impacted-tests.py fails closed
# to a full test run without one. So `codegraph-sync` degrading is correct.
#
# But skipping it silently strands the user later: `litai init`, the skills, and
# the coding-CLI paths all expect CodeGraph, and the failure surfaces far from
# the install that omitted it. Provisioning it here is the cheap fix -- the user
# asked for mac to be installed, and this is part of a working install.
#
# Set MAC_SKIP_CODEGRAPH_INSTALL=1 to decline (air-gapped machines, or an
# operator who wants to choose their own installation method).

CODEGRAPH_BIN=${MAC_CODEGRAPH_BIN:-codegraph}
INSTALLER_URL=${MAC_CODEGRAPH_INSTALLER_URL:-https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh}

if command -v "$CODEGRAPH_BIN" >/dev/null 2>&1; then
    echo "codegraph: already installed ($(command -v "$CODEGRAPH_BIN"))"
    exit 0
fi

if [ "${MAC_SKIP_CODEGRAPH_INSTALL:-0}" = "1" ]; then
    echo "codegraph: not installed, and MAC_SKIP_CODEGRAPH_INSTALL=1; skipping." >&2
    echo "  mac will build and run without it. \`litai init\`, the skills and the" >&2
    echo "  coding-CLI paths expect it, so install it before using those:" >&2
    echo "    curl -fsSL $INSTALLER_URL | sh" >&2
    exit 0
fi

# Stated plainly rather than run quietly: this fetches and executes a script
# from the network, and the operator should be able to see that happening.
echo "codegraph: not found; installing from $INSTALLER_URL"
echo "  (set MAC_SKIP_CODEGRAPH_INSTALL=1 to decline)"

if ! command -v curl >/dev/null 2>&1; then
    echo "codegraph: curl is not available; cannot install automatically." >&2
    echo "  Install CodeGraph manually: $INSTALLER_URL" >&2
    exit 0
fi

if curl -fsSL "$INSTALLER_URL" | sh; then
    # The installer commonly lands in ~/.local/bin, which may not be on PATH
    # for this shell yet. Report the truth rather than assuming success.
    if command -v "$CODEGRAPH_BIN" >/dev/null 2>&1; then
        echo "codegraph: installed ($(command -v "$CODEGRAPH_BIN"))"
    elif [ -x "$HOME/.local/bin/codegraph" ]; then
        echo "codegraph: installed at $HOME/.local/bin/codegraph"
        echo "  It is not on this shell's PATH; add ~/.local/bin to PATH."
    else
        echo "codegraph: installer finished but the binary was not found on PATH." >&2
        echo "  mac is still usable; install it manually before \`litai init\`." >&2
    fi
    exit 0
fi

# A failed install must not block the CLI the user asked for.
echo "codegraph: installation failed; continuing without it." >&2
echo "  mac will build and run. Install it before \`litai init\` or the skills:" >&2
echo "    curl -fsSL $INSTALLER_URL | sh" >&2
exit 0
