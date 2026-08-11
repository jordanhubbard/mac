#!/usr/bin/env bash
set -euo pipefail

DOCUMENT_ID=''
OUTPUT_DIR=''

usage() {
  printf '%s\n' 'Usage: verify_google_doc.sh --document-id ID [--output-dir DIR]'
}

while (($#)); do
  case "$1" in
    --document-id)
      shift
      [[ $# -gt 0 ]] || { usage >&2; exit 2; }
      DOCUMENT_ID="$1"
      ;;
    --output-dir)
      shift
      [[ $# -gt 0 ]] || { usage >&2; exit 2; }
      OUTPUT_DIR="$1"
      ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[[ -n "$DOCUMENT_ID" ]] || { usage >&2; exit 2; }

for command_name in gcloud curl jq pdfinfo pdftoppm magick rg; do
  command -v "$command_name" >/dev/null || {
    printf 'Missing required command: %s\n' "$command_name" >&2
    exit 1
  }
done

if [[ -z "$OUTPUT_DIR" ]]; then
  OUTPUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hgx-runner-doc-qa.XXXXXX")"
else
  mkdir -p "$OUTPUT_DIR"
fi

TOKEN="$(gcloud auth print-access-token)"
DOC_JSON="$OUTPUT_DIR/document.json"
META_JSON="$OUTPUT_DIR/metadata.json"
PDF="$OUTPUT_DIR/document.pdf"
TEXT="$OUTPUT_DIR/readback.txt"
PAGES_DIR="$OUTPUT_DIR/pages"
mkdir -p "$PAGES_DIR"

curl -fsSL -H "Authorization: Bearer ${TOKEN}" \
  "https://docs.googleapis.com/v1/documents/${DOCUMENT_ID}?includeTabsContent=true" \
  -o "$DOC_JSON"
curl -fsSL -H "Authorization: Bearer ${TOKEN}" \
  "https://www.googleapis.com/drive/v3/files/${DOCUMENT_ID}?fields=id,name,modifiedTime,mimeType" \
  -o "$META_JSON"
curl -fsSL -H "Authorization: Bearer ${TOKEN}" \
  "https://www.googleapis.com/drive/v3/files/${DOCUMENT_ID}/export?mimeType=application/pdf" \
  -o "$PDF"
unset TOKEN

jq -r '.. | objects | select(has("textRun")) | .textRun.content' "$DOC_JSON" > "$TEXT"
TABLES="$(jq '[.. | objects | select(has("tableRows"))] | length' "$DOC_JSON")"
[[ "$TABLES" == 6 ]] || { printf 'Expected 6 native tables, found %s\n' "$TABLES" >&2; exit 1; }
rg -q 'Make HGX-Runner the durable, organization-scale control plane' "$TEXT"
rg -q 'MAC is not the target runtime' "$TEXT"
rg -q 'one HGX-owned ledger and API' "$TEXT"

pdftoppm -png -r 110 "$PDF" "$PAGES_DIR/page" >/dev/null 2>&1
page_files=()
while IFS= read -r page_file; do
  page_files+=("$page_file")
done < <(find "$PAGES_DIR" -maxdepth 1 -name 'page-*.png' -print | sort)
PAGE_COUNT="${#page_files[@]}"
[[ "$PAGE_COUNT" -gt 0 ]] || { printf '%s\n' 'PDF render produced no pages.' >&2; exit 1; }

row_files=()
row_number=0
for ((offset=0; offset<PAGE_COUNT; offset+=3)); do
  row_number=$((row_number + 1))
  row_file="$(printf '%s/row-%02d.png' "$PAGES_DIR" "$row_number")"
  row=("${page_files[@]:offset:3}")
  magick "${row[@]}" -thumbnail 260x +append "$row_file"
  row_files+=("$row_file")
done
magick "${row_files[@]}" -append "$OUTPUT_DIR/contact-sheet.png"

jq -n \
  --arg documentId "$DOCUMENT_ID" \
  --arg name "$(jq -r .name "$META_JSON")" \
  --argjson tables "$TABLES" \
  --argjson pages "$PAGE_COUNT" \
  --arg pdf "$PDF" \
  --arg contactSheet "$OUTPUT_DIR/contact-sheet.png" \
  '{documentId:$documentId,name:$name,nativeTables:$tables,pages:$pages,pdf:$pdf,contactSheet:$contactSheet,manualVisualInspectionRequired:true}' \
  | tee "$OUTPUT_DIR/verification-summary.json"

printf 'QA artifacts: %s\n' "$OUTPUT_DIR"
