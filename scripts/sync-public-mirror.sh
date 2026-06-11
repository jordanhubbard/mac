#!/usr/bin/env bash
# Sync the de-personalized public mirror (github.com/NVIDIA-dev/mac) from this
# private source repo. Produces a clean, scrubbed snapshot and pushes it as a
# self-contained commit on the mirror's own history — NO private git history
# crosses over.
#
# Flow:
#   1. `git archive` the source at a SHA  -> only tracked files, no cruft/.venv
#   2. drop personal data files (.tickets, .claude, deploy/*.fleet.yaml)
#   3. depersonalize.py scrub  -> rewrite names/IPs/handles -> placeholders
#   4. depersonalize.py check  -> FAIL-CLOSED gate; never push if a token survives
#   5. (optional) build a venv + run the contract tests on the scrubbed tree
#   6. clone the mirror, replace its tree with the snapshot, commit, push
#
# Usage:
#   scripts/sync-public-mirror.sh [--source-sha SHA] [--remote URL]
#                                 [--run-tests] [--no-push] [--keep-tmp]
#
# Defaults: --source-sha HEAD, --remote git@github.com:NVIDIA-dev/mac.git
set -euo pipefail

SRC_ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
REMOTE="git@github.com:NVIDIA-dev/mac.git"
SOURCE_SHA="HEAD"
RUN_TESTS=0
PUSH=1
KEEP_TMP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --source-sha) SOURCE_SHA="$2"; shift 2;;
    --remote)     REMOTE="$2"; shift 2;;
    --run-tests)  RUN_TESTS=1; shift;;
    --no-push)    PUSH=0; shift;;
    --keep-tmp)   KEEP_TMP=1; shift;;
    -h|--help)    sed -n '2,28p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

# Personal data files that are git-TRACKED but must never reach the mirror.
PERSONAL_PATHS=(
  ".tickets"
  ".claude"
  ".codex"
  ".mac/fleets.yaml"
)

cd "$SRC_ROOT"
SHA_FULL="$(git rev-parse "$SOURCE_SHA")"
SHA_SHORT="$(git rev-parse --short "$SOURCE_SHA")"
echo "==> source: $SRC_ROOT @ $SHA_SHORT"

STAGE="$(mktemp -d)"
MIRROR="$(mktemp -d)"
cleanup() { [ "$KEEP_TMP" = 1 ] || rm -rf "$STAGE" "$MIRROR"; }
trap cleanup EXIT

# 1. tracked tree only (excludes .venv, caches, untracked cruft by construction)
echo "==> exporting tracked tree via git archive"
git archive --format=tar "$SHA_FULL" | tar -x -C "$STAGE"

# 2. strip personal data files + the personal fleet config(s)
echo "==> stripping personal data files"
for p in "${PERSONAL_PATHS[@]}"; do rm -rf "${STAGE:?}/$p"; done
rm -f "$STAGE"/deploy/*.fleet.yaml
# the de-personalization tool itself must not ship in the public mirror
rm -f "$STAGE"/scripts/depersonalize.py "$STAGE"/scripts/sync-public-mirror.sh
rm -f "$STAGE"/docs/public-mirror-sync.md

# 3. scrub
echo "==> scrubbing"
python3 "$SRC_ROOT/scripts/depersonalize.py" scrub "$STAGE"

# 4. fail-closed gate
echo "==> verifying no personal tokens remain"
if ! python3 "$SRC_ROOT/scripts/depersonalize.py" check "$STAGE"; then
  echo "==> ABORT: residual personal tokens found; mirror NOT updated." >&2
  echo "    Extend the mapping in scripts/depersonalize.py and re-run." >&2
  exit 1
fi

# 5. optional test gate (hermetic contract suite, minus DB-only tests)
if [ "$RUN_TESTS" = 1 ]; then
  echo "==> building venv + running contract tests on the scrubbed tree"
  ( cd "$STAGE"
    uv sync --python 3.12 --extra dev --extra k8s --extra hermes-gateway >/dev/null
    bash scripts/run-contract-tests.sh -q --tb=short -m "not postgres" tests/ )
  echo "==> tests passed"
fi

# 6. update the mirror on its own history
echo "==> cloning mirror $REMOTE"
if ! git clone --quiet "$REMOTE" "$MIRROR" 2>/dev/null; then
  echo "    (empty/uninitialized remote — starting fresh history)"
  git -C "$MIRROR" init --quiet -b main
  git -C "$MIRROR" remote add origin "$REMOTE"
fi
git -C "$MIRROR" config user.name  "Dev User"
git -C "$MIRROR" config user.email "dev@example.com"

# replace the mirror's tracked tree with the fresh snapshot
( cd "$MIRROR" && git ls-files -z | xargs -0 -r rm -f )
# copy snapshot (incl. dotfiles) into the mirror clone, preserving its .git
( cd "$STAGE" && tar -cf - . ) | ( cd "$MIRROR" && tar -xf - )

git -C "$MIRROR" add -A
if git -C "$MIRROR" diff --cached --quiet; then
  echo "==> mirror already up to date with $SHA_SHORT — nothing to push"
  exit 0
fi
git -C "$MIRROR" commit --quiet -m "Sync de-personalized snapshot (source $SHA_SHORT)"
echo "==> committed snapshot to mirror"

if [ "$PUSH" = 1 ]; then
  git -C "$MIRROR" push --quiet -u origin main
  echo "==> pushed to $REMOTE (main)"
else
  echo "==> --no-push: mirror staged at $MIRROR (not pushed)"
  KEEP_TMP=1
fi

echo "==> done."
[ "$KEEP_TMP" = 1 ] && echo "    stage=$STAGE  mirror=$MIRROR"
exit 0
