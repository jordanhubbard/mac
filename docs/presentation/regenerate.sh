#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CANONICAL_DOCUMENT_ID='1iinPBrxuP8YtGYsdGCwZ0vlQRgIzU_fCl-CcqnvGnPE'
DOCUMENT_ID="$CANONICAL_DOCUMENT_ID"
APPLY=false
RENAME=false
VERIFY=true

usage() {
  printf '%s\n' \
    'Usage: regenerate.sh --apply [--document-id ID] [--rename] [--skip-verify]' \
    '' \
    'Regenerates a native Google Doc in place. --apply is a required safety flag.' \
    'Without --document-id, the canonical HGX-Runner document is targeted.'
}

while (($#)); do
  case "$1" in
    --apply) APPLY=true ;;
    --document-id)
      shift
      [[ $# -gt 0 ]] || { usage >&2; exit 2; }
      DOCUMENT_ID="$1"
      ;;
    --rename) RENAME=true ;;
    --skip-verify) VERIFY=false ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ "$APPLY" != true ]]; then
  printf '%s\n' 'Refusing to replace a Google Doc body without --apply.' >&2
  exit 2
fi
PYTHON_BIN="${PYTHON:-python3}"
"$PYTHON_BIN" "$SCRIPT_DIR/scripts/update_google_doc.py" --check

update_args=("$DOCUMENT_ID")
if [[ "$RENAME" == true ]]; then
  update_args+=(--rename)
fi
"$PYTHON_BIN" "$SCRIPT_DIR/scripts/update_google_doc.py" "${update_args[@]}"

if [[ "$VERIFY" == true ]]; then
  "$SCRIPT_DIR/scripts/verify_google_doc.sh" --document-id "$DOCUMENT_ID"
fi
