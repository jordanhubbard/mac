#!/usr/bin/env bash
# aiXiv submission script for the fleet paper
# Usage: bash scripts/aixiv_submit.sh [--dry-run]
#
# PREREQUISITE: jkh must run ONCE from their terminal:
#   npx aixiv@latest login
# That opens a browser auth flow and stores the token at ~/.config/aixiv/config.json
# After that this script runs fully autonomously.
#
# Category math.IT confirmed open: https://aixiv.online/api/v1/categories

set -euo pipefail

PAPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEX_FILE="$PAPER_DIR/docs/investigations/arxiv-submission.tex"
PDF_FILE="$PAPER_DIR/docs/investigations/arxiv-submission.pdf"
PROCESS_FILE="$PAPER_DIR/docs/investigations/process-summary.md"
REPO_URL="https://github.com/jordanhubbard/mac"
DRY_RUN="${1:-}"

TOKEN_FILE="$HOME/.config/aixiv/config.json"
if [[ ! -f "$TOKEN_FILE" ]]; then
    echo "ERROR: No aiXiv token. Run once: npx aixiv@latest login"
    exit 1
fi

AIXIV_TOKEN=$(python3 -c "import json; d=json.load(open('$TOKEN_FILE')); print(d.get('token',d.get('access_token','')))")
if [[ -z "$AIXIV_TOKEN" ]]; then
    echo "ERROR: Could not extract token from $TOKEN_FILE"
    exit 1
fi

echo "=== Checking aiXiv account ==="
ACCOUNT=$(curl -s "https://aixiv.online/api/v1/me" -H "Authorization: Bearer $AIXIV_TOKEN")
echo "$ACCOUNT" | python3 -m json.tool

CAN_SUBMIT=$(echo "$ACCOUNT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(str(d.get('can_submit',False)).lower())")
if [[ "$CAN_SUBMIT" != "true" ]]; then
    echo "ERROR: can_submit=false -- verify email at https://aixiv.online/settings"
    exit 1
fi

echo "=== Compiling PDF ==="
cd "$PAPER_DIR/docs/investigations"
pdflatex -interaction=nonstopmode arxiv-submission.tex
pdflatex -interaction=nonstopmode arxiv-submission.tex
echo "PDF compiled: $(du -h arxiv-submission.pdf | cut -f1)"
cd "$PAPER_DIR"

DRY_FLAG=""
if [[ "$DRY_RUN" == "--dry-run" ]]; then
    DRY_FLAG="?dry_run=1"
    echo "=== DRY RUN (no publication) ==="
else
    echo "=== SUBMITTING ==="
fi

RESPONSE=$(curl -s "https://aixiv.online/api/v1/submit${DRY_FLAG}" \
    -H "Authorization: Bearer $AIXIV_TOKEN" \
    -F "source=@${TEX_FILE}" \
    -F "pdf=@${PDF_FILE}" \
    -F "primary_category=math.IT" \
    -F "repo_url=${REPO_URL}" \
    -F "ai_models=Bullwinkle (Hermes/claude-sonnet-4-6), Natasha (Hermes/claude-sonnet-4-6), Rocky (Hermes/claude-sonnet-4-6)" \
    -F "ai_contribution=ai-authored" \
    -F "process_summary=<${PROCESS_FILE}" \
    -F "verification_notes=All measurements from live Postgres queries against agent_brain.soul_nodes at 100.72.16.110:5432. Soul node IDs in Appendix B are durable row identifiers." \
    -F "negative_results=Cosine distance failed to predict cross-pollination (Run 32). Focus-state differential was circular artifact (Runs 45-46). Temporal proximity confound falsified Run 50.")

echo "=== Response ==="
echo "$RESPONSE" | python3 -m json.tool

OK=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(str(d.get('ok',False)).lower())" 2>/dev/null || echo "false")
if [[ "$OK" == "true" ]]; then
    ABS_URL=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('abs_url',''))")
    echo ""
    echo "SUCCESS: $ABS_URL"
    echo "Fleet first autonomous multi-agent scientific publication."
fi
