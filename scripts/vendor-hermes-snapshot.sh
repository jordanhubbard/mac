#!/usr/bin/env bash
#
# Vendor a pinned, pruned snapshot of the Hermes agent runtime into the mac
# monorepo (ADR 0001). This REPLACES the old "git clone pristine upstream +
# apply patches + runtime string surgery" deploy behavior with an owned,
# in-tree snapshot.
#
# Safety: defaults to a DRY RUN. Vendoring ~350k LOC is a deliberate, reviewed
# act (see deploy/hermes/SNAPSHOT.md) — pass --apply to actually write files.
#
# Usage:
#   scripts/vendor-hermes-snapshot.sh                 # dry run at pinned commit
#   scripts/vendor-hermes-snapshot.sh --apply         # vendor at pinned commit
#   scripts/vendor-hermes-snapshot.sh --apply <commit># vendor + bump pin
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SNAPSHOT_DOC="$REPO_ROOT/deploy/hermes/SNAPSHOT.md"
DEST="$REPO_ROOT/src/mac/_hermes"
UPSTREAM="https://github.com/NousResearch/hermes-agent.git"
PATCH_DIR="$REPO_ROOT/deploy/hermes"

APPLY=0
PIN_OVERRIDE=""
FROM=""
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --from=*) FROM="${arg#--from=}" ;;
    --help|-h) sed -n '2,20p' "$0"; exit 0 ;;
    -*) echo "unknown flag: $arg" >&2; exit 2 ;;
    *) PIN_OVERRIDE="$arg" ;;
  esac
done

# Resolve the pin: explicit arg wins, else parse the Commit row from SNAPSHOT.md.
if [ -n "$PIN_OVERRIDE" ]; then
  PIN="$PIN_OVERRIDE"
else
  PIN="$(grep -E '^\| Commit \|' "$SNAPSHOT_DOC" | sed -E 's/.*`([0-9a-f]+)`.*/\1/')"
fi
if [ -z "${PIN:-}" ]; then
  echo "could not resolve Hermes pin commit (check $SNAPSHOT_DOC)" >&2
  exit 1
fi
echo "Hermes snapshot pin: $PIN"

# Include / exclude manifests mirror deploy/hermes/SNAPSHOT.md, derived from
# scripts/trace-hermes-reachability.py (394 reachable first-party files).
# skills/ is runtime-loaded data (not statically imported) so it is included
# explicitly; verify with a runtime trace before trusting the static set.
INCLUDE=(agent gateway providers hermes_cli plugins acp_adapter cron tui_gateway \
  tools skills hermes cli.py mcp_serve.py run_agent.py model_tools.py toolsets.py \
  utils.py hermes_bootstrap.py hermes_time.py hermes_state.py hermes_logging.py \
  hermes_constants.py)
EXCLUDE_GLOBS=(website ui-tui web infographic assets locales \
  datagen-config-examples docs docker nix packaging acp_registry \
  optional-mcps optional-skills plans scripts tests)

if [ "$APPLY" -ne 1 ]; then
  echo
  echo "DRY RUN (pass --apply to write). Would vendor into: $DEST"
  echo "  include: ${INCLUDE[*]}"
  echo "  exclude: ${EXCLUDE_GLOBS[*]}"
  echo "  patches: $(ls "$PATCH_DIR"/*.patch 2>/dev/null | xargs -n1 basename | tr '\n' ' ')"
  echo
  echo "No files written."
  exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
if [ -n "$FROM" ]; then
  # Vendor from a local checkout (must already be at the pinned commit). Avoids
  # a network clone and is what we use when ~/Src/hermes-agent is at the pin.
  echo "Vendoring from local checkout: $FROM"
  mkdir -p "$WORK/hermes"
  rsync -a --exclude='.git' "${FROM%/}/" "$WORK/hermes/"
else
  echo "Cloning upstream at $PIN ..."
  git clone --quiet "$UPSTREAM" "$WORK/hermes"
  git -C "$WORK/hermes" checkout --quiet "$PIN"
fi

echo "Applying in-tree patches ..."
for p in "$PATCH_DIR"/*.patch; do
  [ -e "$p" ] || continue
  git -C "$WORK/hermes" apply --verbose "$p"
done

echo "Pruning excluded trees ..."
for ex in "${EXCLUDE_GLOBS[@]}"; do
  rm -rf "${WORK:?}/hermes/$ex"
done

echo "Copying runtime surface into $DEST ..."
rm -rf "$DEST"
mkdir -p "$DEST"
for inc in "${INCLUDE[@]}"; do
  if [ -e "$WORK/hermes/$inc" ]; then
    cp -rf "$WORK/hermes/$inc" "$DEST/"
  fi
done
# Stamp provenance so the vendored tree is self-describing.
printf 'upstream %s\ncommit %s\nvendored %s\n' \
  "$UPSTREAM" "$PIN" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DEST/SNAPSHOT_PIN"

echo "Done. Review the diff carefully before committing (this is a fork bump)."
