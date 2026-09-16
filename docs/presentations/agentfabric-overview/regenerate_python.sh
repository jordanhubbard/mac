#!/usr/bin/env bash
set -euo pipefail

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(git -C "$source_dir" rev-parse --show-toplevel)"
obj_dir="${OBJ_DIR:-$repo_dir/_build}"
check_only=0
if [[ "${1:-}" == "--check" ]]; then
  check_only=1
fi

python_bin=""
for candidate in \
  "$obj_dir/doc-toolchain/bin/python3" \
  "$obj_dir/doc-toolchain/bin/python" \
  "$obj_dir/doc-toolchain/Scripts/python.exe"
do
  if [[ -x "$candidate" ]]; then
    python_bin="$candidate"
    break
  fi
done
if [[ -z "$python_bin" && -x "$repo_dir/.venv/bin/python3" ]]; then
  python_bin="$repo_dir/.venv/bin/python3"
fi
if [[ -z "$python_bin" ]]; then
  python_bin="python3"
fi

if ! "$python_bin" -c "import pptx, docx, PIL, lxml" >/dev/null 2>&1; then
  echo "Authoring toolchain is not importable for ${python_bin}." >&2
  echo "Install python-pptx, python-docx, lxml, and Pillow into a virtualenv and point" >&2
  echo "OBJ_DIR/doc-toolchain (or .venv) at it; this script will not pip-install for you." >&2
  exit 2
fi

if [[ "$check_only" -eq 1 ]]; then
  printf '%s\n' "$python_bin"
  exit 0
fi

build_dir="$obj_dir/agentfabric-overview"
mkdir -p "$build_dir"

(
  cd "$source_dir"
  AGENTFABRIC_DECK_SOURCE="$source_dir" \
  AGENTFABRIC_REPO="$repo_dir" \
  OBJ_DIR="$obj_dir" \
  "$python_bin" build_deck.py
  AGENTFABRIC_DECK_SOURCE="$source_dir" \
  AGENTFABRIC_REPO="$repo_dir" \
  OBJ_DIR="$obj_dir" \
  "$python_bin" build_narrative.py
  if [[ "${AGENTFABRIC_DECK_SKIP_RENDER:-}" != "1" ]]; then
    AGENTFABRIC_DECK_SOURCE="$source_dir" \
    AGENTFABRIC_REPO="$repo_dir" \
    OBJ_DIR="$obj_dir" \
    "$python_bin" render_slides.py
  fi
)

pptx="${AGENTFABRIC_DECK_OUTPUT:-$source_dir/agentfabric-overview.pptx}"
docx="${AGENTFABRIC_NARRATIVE_OUTPUT:-$source_dir/agentfabric-overview.docx}"
echo "PPTX: $pptx"
echo "DOCX: $docx"

AGENTFABRIC_DECK_SOURCE="$source_dir" \
AGENTFABRIC_REPO="$repo_dir" \
OBJ_DIR="$obj_dir" \
"$python_bin" "$source_dir/verify_pair.py"
echo "Acceptance report: $build_dir/acceptance.json"
