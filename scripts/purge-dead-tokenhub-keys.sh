#!/usr/bin/env bash
# purge-dead-tokenhub-keys.sh — remove retired-TokenHub env leftovers from the
# env files on THIS machine (agent and/or operator).
#
# TokenHub was retired (th-merge-07: the in-mac router replaced it). These vars
# are dead plaintext liabilities that nothing in the current code writes; only
# the retirement-handling/compat paths read them, and they resolve correctly to
# empty once removed. Carrying them — especially a TokenHub admin token or the
# root URL that still influences provider/runtime resolution — is pure attack
# surface for zero function.
#
# Cleans, when present:
#   - $HOME/.mac/mac.env                          (agent systemd EnvironmentFile)
#   - ${MAC_DEPLOY_ENV_FILE:-$HOME/.mac/.env}     (operator deploy env)
#   - $HOME/.hermes/.env                          (gateway env)
#
# Idempotent; backs up each modified file (cp -pf) before rewriting (awk so an
# all-dead file is emptied, not aborted under set -e); prints only key NAMES.
#
# Run locally:  bash purge-dead-tokenhub-keys.sh [--dry-run]
# NOTE: a TokenHub *admin* token exposed here must ALSO be rotated/revoked at the
# source — deleting the local copy does not invalidate the token itself.
set -euo pipefail

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

# Dead retired-TokenHub vars. The regex also matches an optional `export ` prefix
# and an optional fleet-scoped `__SUFFIX` (e.g. MAC_DEPLOY_TOKENHUB_API_KEY__HOSTA).
PAT='^(export )?(TOKENHUB_API_KEY|TOKENHUB_ADMIN_TOKEN|TOKENHUB_AGENT_KEY|TOKENHUB_URL|MAC_REQUIRE_TOKENHUB|MAC_TOKENHUB_PORT|MAC_TOKENHUB_URL|MAC_DEPLOY_TOKENHUB_API_KEY|MAC_DEPLOY_TOKENHUB_URL|MAC_DEPLOY_TOKENHUB_PORT|MAC_DEPLOY_TOKENHUB_INSTALL|MAC_DEPLOY_TOKENHUB_REF)(__[A-Za-z0-9_]+)?='

TS="$(date -u +%Y%m%dT%H%M%SZ)"
changed=0
processed=""

for f in "$HOME/.mac/mac.env" "${MAC_DEPLOY_ENV_FILE:-$HOME/.mac/.env}" "$HOME/.hermes/.env"; do
  # Skip a path we've already handled (e.g. MAC_DEPLOY_ENV_FILE == mac.env).
  case " $processed " in *" $f "*) continue ;; esac
  processed="$processed $f"
  if [ ! -f "$f" ]; then
    echo "  $f: absent"
    continue
  fi
  hits="$(grep -cE "$PAT" "$f" 2>/dev/null || true)"
  if [ "${hits:-0}" -eq 0 ]; then
    echo "  $f: clean"
    continue
  fi
  echo "  $f: removing $hits dead TokenHub line(s):"
  grep -nE "$PAT" "$f" | sed -E 's/=.*/=<redacted>/' | sed 's/^/      /'
  [ "$DRY" = "1" ] && continue
  cp -pf "$f" "$f.bak-tokenhub-$TS"
  # awk (not grep -v): always exits 0, so a file of ONLY dead lines is emptied
  # rather than aborting the script after the backup.
  awk -v p="$PAT" '$0 !~ p' "$f" > "$f.purge.$$"
  chmod 600 "$f.purge.$$"
  mv -f "$f.purge.$$" "$f"
  changed=1
done

echo "PURGE_CHANGED=$changed"
[ "$DRY" = "1" ] && echo "(dry-run; no files modified)"
exit 0
