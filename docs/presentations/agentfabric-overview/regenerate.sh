#!/usr/bin/env bash
set -euo pipefail

# Entry point for regenerating the AgentFabric overview pair. Everything is built
# by the pinned python-pptx / python-docx toolchain; generated artifacts land in
# this directory and QA output lands in the ignored OBJ_DIR.

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(git -C "$source_dir" rev-parse --show-toplevel)"
obj_dir="${OBJ_DIR:-$repo_dir/_build}"

exec env OBJ_DIR="$obj_dir" "$source_dir/regenerate_python.sh" "$@"
