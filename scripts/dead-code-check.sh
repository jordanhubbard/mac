#!/usr/bin/env bash
# Dead-code contract gate.
#
# Fails on unreachable / unused code at >=90% confidence in the MAC-owned source
# (src/mac, excluding the vendored _hermes runtime), measured with vulture and
# suppressed only by scripts/vulture_allowlist.py — a vetted list of genuine
# false positives (framework hooks, dynamic dispatch, interface no-op params).
#
# Motivation: a "disk cleanup" feature was once "implemented" as
# scripts/cleanup_artifacts.py but never wired to any scheduler, then deleted —
# its absence let worker disks fill and starve. This gate catches the
# "implemented but never reached" pattern automatically on every push instead of
# letting dead code accumulate and confuse future readers.
#
# Genuine dead code must be DELETED, not allowlisted. Only add a genuine false
# positive to the allowlist, by regenerating it:
#     scripts/dead-code-check.sh --make-whitelist
set -euo pipefail
cd "$(dirname "$0")/.."

SCOPE="src/mac"
ALLOW="scripts/vulture_allowlist.py"
CONF="${MAC_DEAD_CODE_MIN_CONFIDENCE:-90}"
EXCLUDE="*_hermes*"   # vendored Hermes runtime is audited separately

vulture() { uv run --with vulture vulture "$@"; }

if [ "${1:-}" = "--make-whitelist" ]; then
    header="$(sed -n '1,6p' "$ALLOW" 2>/dev/null || true)"
    { [ -n "$header" ] && printf '%s\n\n' "$header"; \
      vulture "$SCOPE" --min-confidence "$CONF" --exclude "$EXCLUDE" --make-whitelist; \
    } > "$ALLOW.tmp"
    mv "$ALLOW.tmp" "$ALLOW"
    echo "regenerated $ALLOW"
    exit 0
fi

if vulture "$SCOPE" "$ALLOW" --min-confidence "$CONF" --exclude "$EXCLUDE"; then
    echo "dead-code-check: clean (no unreachable code >=${CONF}% outside the allowlist)"
else
    status=$?
    echo "" >&2
    echo "dead-code-check: FAILED — new dead / unreachable code above." >&2
    echo "  Fix by DELETING the dead code." >&2
    echo "  Only if it is a genuine false positive (framework hook / dynamic" >&2
    echo "  dispatch / interface no-op param), regenerate the vetted allowlist:" >&2
    echo "      scripts/dead-code-check.sh --make-whitelist" >&2
    exit "$status"
fi
